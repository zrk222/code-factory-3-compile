"""Self-healing harness: scaffold, ledger and protected-file integrity, guarded repair loop, receipts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from hsf.cli import main
from hsf.harness import Ledger, load_harness
from hsf.harness.gates import classify, run_gate, run_gates, signature
from hsf.harness.heal import heal
from hsf.harness.scaffold import init_harness, protected_drift
from hsf.harness.spec import GateSpec

PY = f'"{sys.executable}"'
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
GATE = PY + " -c \"import sys; sys.path.insert(0, 'src'); import calc; sys.exit(0 if calc.add(2, 2) == 4 else 1)\""


def _app(tmp_path: Path, source: str = BUGGY, repair: str | None = None, **spec_updates) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(source)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    init_harness("calc", tmp_path, target="generic", with_cf=False)
    spec_file = tmp_path / "harness" / "harness.yaml"
    payload = yaml.safe_load(spec_file.read_text())
    payload["gates"] = [{"id": "unit", "kind": "unit", "run": GATE, "flake_retries": 0}]
    payload["repair"] = {"command": repair, "max_attempts": 3}
    payload.update(spec_updates)
    spec_file.write_text(yaml.safe_dump(payload))
    from hsf.harness.scaffold import seal_protected
    seal_protected(tmp_path, load_harness(tmp_path))
    return tmp_path


def _writer(tmp_path: Path, name: str, target: str, text: str) -> str:
    script = tmp_path / ".hsf-harness" / f"{name}.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"from pathlib import Path\nPath({target!r}).write_text({text!r})\n")
    return f'{PY} "{script}"'


def test_init_scaffolds_sealed_harness(tmp_path):
    created = init_harness("shop", tmp_path, target="web", with_cf=False)
    assert all(path.exists() for path in created)
    spec = load_harness(tmp_path)
    assert [g.id for g in spec.gates] == ["lint", "unit", "e2e"]
    assert Ledger(tmp_path / spec.features_path).next_feature().id == "F001"
    assert protected_drift(tmp_path, spec) == []
    assert "Never edit, delete or weaken tests" in (tmp_path / "AGENTS.md").read_text()
    with pytest.raises(FileExistsError):
        init_harness("shop", tmp_path)


def test_ledger_rejects_edits_outside_passes(tmp_path):
    _app(tmp_path)
    spec = load_harness(tmp_path)
    path = tmp_path / spec.features_path
    rows = json.loads(path.read_text())
    rows[0]["passes"] = True
    path.write_text(json.dumps(rows))
    Ledger(path).verify()
    rows[0]["description"] = "weakened"
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="outside `passes`"):
        Ledger(path).verify()


def test_heal_reports_healthy_without_repair(tmp_path):
    _app(tmp_path, source=FIXED)
    receipt = heal(tmp_path, load_harness(tmp_path))
    assert receipt["status"] == "healthy" and receipt["repair_attempts"] == 0
    assert Path(receipt["path"]).exists()


def test_heal_keeps_a_guarded_fix_and_receipts_it(tmp_path):
    app = _app(tmp_path)
    app_repair = _writer(tmp_path, "fix", "src/calc.py", FIXED)
    spec = load_harness(app)
    spec.repair.command = app_repair
    receipt = heal(app, spec)
    assert receipt["status"] == "healthy" and receipt["stop_reason"] == "healthy"
    kept = receipt["rounds"][-1]
    assert kept["action"] == "keep" and list(kept["kept_changes"]) == ["src/calc.py"]
    assert (app / "src" / "calc.py").read_text() == FIXED
    log = (app / "harness" / "progress.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["status"] == "healthy"


def test_heal_rolls_back_a_repair_that_weakens_tests(tmp_path):
    app = _app(tmp_path)
    test_file = app / "tests" / "test_calc.py"
    original = test_file.read_bytes()
    spec = load_harness(app)
    spec.repair.command = _writer(tmp_path, "weaken", "tests/test_calc.py", "def test_add():\n    pass\n")
    receipt = heal(app, spec)
    assert receipt["status"] == "unhealthy"
    assert {row["action"] for row in receipt["rounds"][1:]} == {"rollback:guard"}
    assert receipt["rounds"][1]["guard"]["protected_touched"] == ["tests/test_calc.py"]
    assert test_file.read_bytes() == original
    assert protected_drift(app, spec) == []


def test_heal_rolls_back_edits_outside_mutable_paths(tmp_path):
    app = _app(tmp_path)
    spec = load_harness(app)
    spec.repair.command = _writer(tmp_path, "stray", "setup.py", "print('x')\n")
    receipt = heal(app, spec)
    assert receipt["rounds"][1]["guard"]["outside_mutable"] == ["setup.py"]
    assert not (app / "setup.py").exists()


def test_heal_rolls_back_a_repair_that_does_not_help(tmp_path):
    app = _app(tmp_path)
    spec = load_harness(app)
    spec.repair.command = _writer(tmp_path, "noop", "src/calc.py", BUGGY)
    spec.repair.max_attempts = 2
    receipt = heal(app, spec)
    assert receipt["stop_reason"] == "max_attempts"
    assert (app / "src" / "calc.py").read_text() == BUGGY


def test_heal_stops_on_oscillation(tmp_path):
    app = _app(tmp_path)
    counter = tmp_path / ".hsf-harness" / "count"
    flip = tmp_path / ".hsf-harness" / "flip.py"
    flip.parent.mkdir(exist_ok=True)
    flip.write_text(
        "from pathlib import Path\n"
        f"c = Path({str(counter)!r}); n = int(c.read_text()) if c.exists() else 0; c.write_text(str(n + 1))\n"
        "Path('src/calc.py').write_text('def add(a, b):\\n    raise ValueError(\"x\")\\n' if n % 2 == 0 else "
        f"{BUGGY!r})\n")
    spec = load_harness(app)
    spec.repair.command = f'{PY} "{flip}"'
    receipt = heal(app, spec)
    actions = [row["action"] for row in receipt["rounds"][1:]]
    assert actions[0] == "keep" and actions[-1] == "rollback:oscillation"
    assert receipt["stop_reason"] == "oscillation"


def test_environment_failure_is_healed_by_reinit(tmp_path):
    marker = "ready.flag"
    app = _app(tmp_path, source=FIXED, init=[PY + f" -c \"open('{marker}', 'w').write('1')\""])
    spec = load_harness(app)
    spec.gates[0] = GateSpec(id="unit", kind="unit", flake_retries=0,
                             run=PY + f" -c \"import os, sys; sys.exit(0 if os.path.exists('{marker}') else 127)\"")
    receipt = heal(app, spec)
    assert receipt["status"] == "healthy"
    assert receipt["rounds"][1]["round"] == "reinit"


def test_flaky_gate_is_classified_not_failed(tmp_path):
    counter = tmp_path / "n"
    gate = GateSpec(id="e2e", kind="e2e", flake_retries=1, run=PY + " -c \"from pathlib import Path; "
                    f"p = Path(r'{counter}'); n = int(p.read_text()) if p.exists() else 0; p.write_text(str(n + 1)); "
                    "raise SystemExit(0 if n else 1)\"")
    run = run_gate(gate, tmp_path)
    assert run.passed and run.classification == "flake" and run.attempts == 2


def test_classification_and_signature_are_stable():
    assert classify(127, "") == "environment"
    assert classify(1, "ModuleNotFoundError: No module named 'x'") == "environment"
    assert classify(1, "SyntaxError: invalid syntax") == "build"
    assert classify(1, "AssertionError: 3 != 4") == "verification"
    assert classify(None, "") == "timeout"
    assert signature("u", "verification", "took 12 ms\nFAIL x") == signature("u", "verification", "took 99 ms\nFAIL x")


def test_cli_pass_only_after_gates_and_tamper_exit_codes(tmp_path, capsys):
    app = _app(tmp_path)
    root = ["--root", str(app)]
    with pytest.raises(SystemExit) as refused:
        main(["harness", "pass", "F001", *root])
    assert refused.value.code == 1
    (app / "src" / "calc.py").write_text(FIXED)
    main(["harness", "pass", "F001", *root])
    main(["harness", "status", "--json", *root])
    status = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert status["passing"] == 1 and status["protected_ok"] and status["next_feature"] is None
    (app / "tests" / "test_calc.py").write_text("def test_add():\n    pass\n")
    with pytest.raises(SystemExit) as tampered:
        main(["harness", "heal", *root])
    assert tampered.value.code == 3


def test_cli_add_feature_reseals(tmp_path):
    app = _app(tmp_path, source=FIXED)
    main(["harness", "add-feature", "F002", "--description", "adds numbers", "--step", "add(2, 2) == 4",
          "--gate", "unit", "--root", str(app)])
    spec = load_harness(app)
    assert [f.id for f in Ledger(app / spec.features_path).verify()] == ["F001", "F002"]
    assert protected_drift(app, spec) == []


def test_gate_cache_reuses_unchanged_inputs_and_invalidates_on_change(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(FIXED)
    counter = tmp_path / "runs"
    gate = GateSpec(id="unit", kind="unit", flake_retries=0, inputs=["src/**"],
                    run=PY + f" -c \"from pathlib import Path; p = Path(r'{counter}'); "
                    "p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))\"")
    first, = run_gates([gate], tmp_path)
    second, = run_gates([gate], tmp_path)
    assert first.classification == "ok" and second.classification == "cached" and counter.read_text() == "1"
    (tmp_path / "src" / "calc.py").write_text(BUGGY)
    third, = run_gates([gate], tmp_path)
    assert third.classification == "ok" and counter.read_text() == "2"
    run_gates([gate], tmp_path, use_cache=False)
    assert counter.read_text() == "3"


def test_parallel_gates_keep_declared_order(tmp_path):
    gates = [GateSpec(id=f"g{i}", kind="unit", flake_retries=0, run=PY + f" -c \"import time; time.sleep({0.3 - i * 0.1})\"")
             for i in range(3)]
    runs = run_gates(gates, tmp_path, parallel=3, use_cache=False)
    assert [run.gate for run in runs] == ["g0", "g1", "g2"] and all(run.passed for run in runs)


def test_code_factory_gates_are_added_only_when_installed(tmp_path):
    from hsf.harness.cf import available_cf_gates
    assert available_cf_gates(lambda tool: None) == []
    ids = [gate["id"] for gate in available_cf_gates(lambda tool: f"/usr/bin/{tool}")]
    assert ids == ["cf-quality", "cf-scan"]
    gates = {gate["id"]: gate for gate in available_cf_gates(lambda tool: "/x")}
    assert gates["cf-quality"]["required"] is True and gates["cf-scan"]["required"] is False
    assert "factory wrap" in (init_harness("app", tmp_path, with_cf=True) and (tmp_path / "AGENTS.md").read_text())


def test_cli_seal_records_owner_edits(tmp_path):
    app = _app(tmp_path, source=FIXED)
    spec_file = app / "harness" / "harness.yaml"
    spec_file.write_text(spec_file.read_text() + "\n")
    assert protected_drift(app, load_harness(app)) == ["harness/harness.yaml"]
    main(["harness", "seal", "--reason", "configure gates", "--root", str(app)])
    assert protected_drift(app, load_harness(app)) == []
    last = json.loads((app / "harness" / "progress.jsonl").read_text().splitlines()[-1])
    assert last["event"] == "seal" and last["files"] == ["harness/harness.yaml"] and last["reason"] == "configure gates"
