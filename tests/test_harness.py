"""Self-healing harness: scaffold, ledger and protected-file integrity, guarded repair loop, receipts."""
from __future__ import annotations

import json
import os
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


def test_heal_rolls_back_a_repair_that_only_changes_the_failure(tmp_path):
    """HH-5: a different error in the same failing gate is not progress; the tree ends as it started."""
    app = _app(tmp_path)
    spec = load_harness(app)
    spec.repair.command = _writer(tmp_path, "other", "src/calc.py", 'def add(a, b):\n    raise ValueError("x")\n')
    spec.repair.max_attempts = 2
    receipt = heal(app, spec)
    assert {row["action"] for row in receipt["rounds"][1:]} == {"rollback:no_improvement"}
    assert receipt["status"] == "unhealthy" and (app / "src" / "calc.py").read_text() == BUGGY


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
    assert gates["cf-quality"]["required"] is False and gates["cf-scan"]["required"] is False
    scoped = {g["id"]: g for g in available_cf_gates(lambda tool: "/x", feature="shop", ssat="shop.ssat.yaml")}
    assert scoped["cf-quality"]["required"] is True and "--ssat shop.ssat.yaml" in scoped["cf-quality"]["run"]
    assert available_cf_gates(lambda tool: None, mode="on") and available_cf_gates(lambda tool: "/x", mode="off") == []
    assert "factory wrap" in (init_harness("app", tmp_path, with_cf=True) and (tmp_path / "AGENTS.md").read_text())


def test_cli_seal_records_owner_edits(tmp_path):
    app = _app(tmp_path, source=FIXED)
    spec_file = app / "harness" / "harness.yaml"
    spec_file.write_text(spec_file.read_text() + "\n")
    assert protected_drift(app, load_harness(app)) == ["harness/harness.yaml"]
    main(["harness", "seal", "--reason", "configure gates", "--approver", "owner", "--yes", "--root", str(app)])
    assert protected_drift(app, load_harness(app)) == []
    last = json.loads((app / "harness" / "progress.jsonl").read_text().splitlines()[-1])
    assert last["event"] == "seal" and last["files"] == ["harness/harness.yaml"] and last["reason"] == "configure gates"
    assert last["approver"] == "owner"


# --- Hardening: each test reproduces a reported way to weaken the oracle unnoticed. ---

def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / ".hsf-harness" / f"{name}.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("import os\nfrom pathlib import Path\n" + body)
    return f'{PY} "{script}"'


def test_pass_and_add_feature_refuse_when_a_test_was_weakened(tmp_path):
    """HH-1: marking a feature must not re-seal a protected file someone weakened."""
    app = _app(tmp_path, source=FIXED)
    lock = app / "harness" / "protected.lock.json"
    sealed = lock.read_bytes()
    (app / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    for argv in (["harness", "pass", "F001"],
                 ["harness", "add-feature", "F009", "--description", "x", "--step", "y", "--gate", "unit"]):
        with pytest.raises(SystemExit) as refused:
            main([*argv, "--root", str(app)])
        assert refused.value.code == 3
    assert lock.read_bytes() == sealed
    assert protected_drift(app, load_harness(app)) == ["tests/test_calc.py"]


def test_check_refuses_to_run_gates_over_broken_seals(tmp_path):
    """HH-4: a CI `check` must notice an edited test before reporting green."""
    app = _app(tmp_path, source=FIXED)
    (app / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n")
    with pytest.raises(SystemExit) as refused:
        main(["harness", "check", "--root", str(app)])
    assert refused.value.code == 3
    main(["harness", "check", "--allow-drift", "--root", str(app)])


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="symlinks unavailable")
def test_symlinked_conftest_that_skips_tests_is_rolled_back(tmp_path):
    """HH-2: a mutable file linked into the test tree must not pass the guard."""
    app = _app(tmp_path)
    spec = load_harness(app)
    spec.repair.command = _script(tmp_path, "link", (
        "Path('src/_hook.py').write_text('import pytest\\n')\n"
        "Path('src/calc.py').write_text(" + repr(FIXED) + ")\n"
        "os.symlink(os.path.join('..', 'src', '_hook.py'), os.path.join('tests', 'conftest.py'))\n"))
    try:
        receipt = heal(app, spec)
    except OSError:
        pytest.skip("symlink creation not permitted")
    row = receipt["rounds"][1]
    assert row["action"] == "rollback:guard" and "tests/conftest.py" in row["guard"]["protected_touched"]
    assert not (app / "tests" / "conftest.py").is_symlink() and not (app / "src" / "_hook.py").exists()


def test_writes_into_skipped_directories_are_seen_and_undone(tmp_path):
    """HH-2: .venv/ and build/ are not digested file by file, but a repair writing there is still caught."""
    app = _app(tmp_path)
    (app / ".venv").mkdir()
    (app / ".venv" / "existing.txt").write_text("keep")
    spec = load_harness(app)
    spec.repair.command = _script(tmp_path, "venv", (
        "Path('src/calc.py').write_text(" + repr(FIXED) + ")\n"
        "Path('.venv/conftest_plugin.py').write_text('x')\n"))
    receipt = heal(app, spec)
    row = receipt["rounds"][1]
    assert row["action"] == "rollback:guard" and ".venv/" in row["guard"]["outside_mutable"]
    assert not (app / ".venv" / "conftest_plugin.py").exists() and (app / ".venv" / "existing.txt").exists()


def test_protected_files_inside_skip_named_directories_are_sealed(tmp_path):
    """HH-2: tests/build/** is still protected even though `build` directories are normally skipped."""
    app = _app(tmp_path, source=FIXED)
    (app / "tests" / "build").mkdir()
    (app / "tests" / "build" / "test_more.py").write_text("def test_x():\n    assert 1\n")
    from hsf.harness.scaffold import seal_protected
    seal_protected(app, load_harness(app))
    (app / "tests" / "build" / "test_more.py").write_text("def test_x():\n    pass\n")
    assert protected_drift(app, load_harness(app)) == ["tests/build/test_more.py"]


def test_a_forged_gate_cache_cannot_heal(tmp_path):
    """HH-3: a repair that only writes a passing cache entry must not make the run healthy."""
    app = _app(tmp_path)
    spec_file = app / "harness" / "harness.yaml"
    payload = yaml.safe_load(spec_file.read_text())
    payload["gates"][0]["inputs"] = ["src/**"]
    spec_file.write_text(yaml.safe_dump(payload))
    from hsf.harness.scaffold import seal_protected
    seal_protected(app, load_harness(app))
    spec = load_harness(app)
    from hsf.harness.gates import CACHE_PATH, cache_key
    forged = json.dumps({"unit": cache_key(spec.gates[0], app)})
    spec.repair.command = _script(tmp_path, "forge", f"Path({CACHE_PATH!r}).write_text({forged!r})\n")
    receipt = heal(app, spec)
    assert receipt["status"] == "unhealthy"
    assert receipt["rounds"][1]["action"] == "rollback:guard"
    assert ".hsf-harness/gate-cache.json" in receipt["rounds"][1]["guard"]["protected_touched"]
    assert ".hsf-harness/" in (app / ".gitignore").read_text()


def test_code_factory_evidence_is_append_only_for_repairs(tmp_path):
    """HH-6: new tool evidence (factory wrap, doctor) is allowed; rewriting existing evidence is not."""
    app = _app(tmp_path)
    (app / "receipts").mkdir()
    (app / "receipts" / "old.json").write_text("{}")
    spec = load_harness(app)
    spec.repair.command = _script(tmp_path, "cf", (
        "Path('src/calc.py').write_text(" + repr(FIXED) + ")\n"
        "Path('.factory/ops').mkdir(parents=True, exist_ok=True)\n"
        "Path('.factory/ops/lifecycle.json').write_text('{}')\n"))
    kept = heal(app, spec)
    assert kept["status"] == "healthy" and (app / ".factory" / "ops" / "lifecycle.json").exists()
    (app / "src" / "calc.py").write_text(BUGGY)
    spec.repair.command = _script(tmp_path, "rewrite", (
        "Path('src/calc.py').write_text(" + repr(FIXED) + ")\n"
        "Path('receipts/old.json').write_text('{\"forged\": true}')\n"))
    rejected = heal(app, spec)
    assert rejected["rounds"][1]["action"] == "rollback:guard"
    assert rejected["rounds"][1]["guard"]["evidence_rewritten"] == ["receipts/old.json"]
    assert (app / "receipts" / "old.json").read_text() == "{}"


def test_pass_refuses_a_flaky_gate(tmp_path):
    """A feature is marked only on a clean, non-flaky pass."""
    app = _app(tmp_path, source=FIXED)
    counter = tmp_path / "flaky-count"
    spec_file = app / "harness" / "harness.yaml"
    payload = yaml.safe_load(spec_file.read_text())
    payload["gates"] = [{"id": "unit", "kind": "unit", "flake_retries": 1, "run": PY + " -c \"from pathlib import Path; "
                         f"p = Path(r'{counter}'); n = int(p.read_text()) if p.exists() else 0; p.write_text(str(n + 1)); "
                         "raise SystemExit(0 if n % 2 else 1)\""}]
    spec_file.write_text(yaml.safe_dump(payload))
    from hsf.harness.scaffold import seal_protected
    seal_protected(app, load_harness(app))
    with pytest.raises(SystemExit) as refused:
        main(["harness", "pass", "F001", "--root", str(app)])
    assert refused.value.code == 1


def test_harness_import_is_lazy():
    """HH-11: non-harness commands do not import the harness modules or pydantic through it."""
    import subprocess
    code = ("import sys, hsf.harness.cli; "
            "sys.exit(int(any(m in sys.modules for m in ('hsf.harness.spec', 'hsf.harness.ledger', 'pydantic'))))")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


# --- HS-5 / HS-6: remaining audit items ---

def _fake_factory(tmp_path: Path, monkeypatch, body: str) -> Path:
    """A `factory` executable on PATH that runs `body` (Python) with argv in `args`."""
    if os.name != "posix":
        pytest.skip("fake executables need POSIX")
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "factory"
    log = tmp_path / "factory-calls.jsonl"
    exe.write_text(f"#!{sys.executable}\nimport json, sys\nargs = sys.argv[1:]\n"
                   f"open({str(log)!r}, 'a').write(json.dumps(args) + '\\n')\n{body}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_flake_retries_default_to_zero_for_deterministic_kinds():
    assert GateSpec(id="g", run="x", kind="golden").flake_retries == 0
    assert GateSpec(id="g", run="x", kind="lint").flake_retries == 0
    assert GateSpec(id="g", run="x", kind="e2e").flake_retries == 1


def test_gate_timeout_kills_the_whole_process_group(tmp_path):
    if os.name != "posix":
        pytest.skip("process groups are POSIX")
    pidfile = tmp_path / "child.pid"
    gate = GateSpec(id="hang", kind="e2e", timeout_s=1, flake_retries=0,
                    run=f"sleep 30 & echo $! > {pidfile}; wait")
    run = run_gate(gate, tmp_path)
    assert run.classification == "timeout"
    import time
    time.sleep(0.2)
    pid = int(pidfile.read_text())
    stat = Path(f"/proc/{pid}/stat")
    # Gone, or a zombie awaiting reaping: either way it is no longer running.
    assert not stat.exists() or stat.read_text().split(") ", 1)[1].split()[0] in ("Z", "X")


def test_secrets_are_redacted_from_gate_output(tmp_path):
    gate = GateSpec(id="leak", kind="unit", flake_retries=0,
                    run=PY + " -c \"print('api_key=abcdef123456 ghp_' + 'a' * 30); raise SystemExit(1)\"")
    run = run_gate(gate, tmp_path)
    assert "abcdef123456" not in run.output_tail and "ghp_aaaa" not in run.output_tail and "***" in run.output_tail


def test_receipts_are_unique_v2_and_the_progress_log_is_chained(tmp_path):
    from hsf.harness.heal import verify_progress_chain
    app = _app(tmp_path, source=FIXED)
    spec = load_harness(app)
    first, second = heal(app, spec), heal(app, spec)
    assert first["path"] != second["path"] and first["schema"] == "hsf.harness-heal.v2"
    assert first["harness_sha256"] and first["protected_lock_sha256"] and first["tool"]["package"]
    assert verify_progress_chain(app, spec) == []
    log = app / "harness" / "progress.jsonl"
    lines = log.read_text().splitlines()
    lines[0] = lines[0].replace("healthy", "HEALTHY")
    log.write_text("\n".join(lines) + "\n")
    assert verify_progress_chain(app, spec) == [2]


def test_unhealthy_run_restores_the_start_state_unless_keep_partial(tmp_path):
    app = _app(tmp_path)
    spec_file = app / "harness" / "harness.yaml"
    payload = yaml.safe_load(spec_file.read_text())
    payload["gates"].append({"id": "never", "kind": "unit", "run": PY + " -c \"raise SystemExit(1)\"", "flake_retries": 0})
    spec_file.write_text(yaml.safe_dump(payload))
    from hsf.harness.scaffold import seal_protected
    seal_protected(app, load_harness(app))
    spec = load_harness(app)
    spec.repair.command = _writer(tmp_path, "half", "src/calc.py", FIXED)
    spec.repair.max_attempts = 1
    reverted = heal(app, spec)
    assert reverted["status"] == "unhealthy" and reverted["rounds"][1]["action"] == "keep"
    assert "src/calc.py" in reverted["reverted_to_start"] and (app / "src" / "calc.py").read_text() == BUGGY
    kept = heal(app, spec, keep_partial=True)
    assert kept["reverted_to_start"] == [] and (app / "src" / "calc.py").read_text() == FIXED


def test_agent_rules_are_a_block_in_an_existing_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Project rules\nKeep it tidy.\n")
    init_harness("svc", tmp_path, target="api", with_cf=False)
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.startswith("# Project rules") and "<!-- hsf-harness:start -->" in text and "weaken tests" in text
    from hsf.harness.scaffold import write_agent_rules
    write_agent_rules(tmp_path, "svc")
    assert (tmp_path / "AGENTS.md").read_text().count("<!-- hsf-harness:start -->") == 1


def test_init_refuses_gates_whose_npm_script_is_missing(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint ."}}))
    with pytest.raises(ValueError, match="'test' script"):
        init_harness("shop", tmp_path, target="web", with_cf=False)


def test_check_resets_a_regressed_feature_and_reports_attribution(tmp_path, capsys):
    app = _app(tmp_path, source=FIXED)
    root = ["--root", str(app)]
    main(["harness", "pass", "F001", *root])
    (app / "src" / "calc.py").write_text(BUGGY)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["harness", "check", "--json", "--no-cache", *root])
    payload = json.loads(capsys.readouterr().out)
    assert payload["regressed_features"] == ["F001"]
    assert payload["attribution"]["units"][0]["failure_class"] == "wrong_output"
    assert not Ledger(app / "harness" / "features.json").verify()[0].passes


def test_heal_verify_gates_kept_repairs(tmp_path, monkeypatch):
    app = _app(tmp_path)
    spec = load_harness(app)
    spec.repair.command = _writer(tmp_path, "fix", "src/calc.py", FIXED)
    spec.repair.verify_with = "factory-journey"
    _fake_factory(tmp_path, monkeypatch, "print(json.dumps({'decision': 'passed', 'receipt_sha256': 'x'}))")
    accepted = heal(app, spec)
    assert accepted["status"] == "healthy" and accepted["rounds"][1]["heal_verify"]["decision"] == "passed"
    healing = json.loads(next((app / ".hsf-harness").glob("healing-*.json")).read_text())
    assert set(healing) == {"schema", "healing_id", "review_mode", "agent", "patch", "allowed_paths",
                            "semantic_identity", "coverage_before", "coverage_after", "positive_argv",
                            "negative_argv"}
    (app / "src" / "calc.py").write_text(BUGGY)
    _fake_factory(tmp_path, monkeypatch, "print(json.dumps({'decision': 'rejected'}))")
    rejected = heal(app, spec)
    assert rejected["rounds"][1]["action"] == "rollback:heal_verify" and (app / "src" / "calc.py").read_text() == BUGGY


def test_heal_verify_fails_closed_without_factory(tmp_path, monkeypatch):
    app = _app(tmp_path)
    spec = load_harness(app)
    spec.repair.command = _writer(tmp_path, "fix", "src/calc.py", FIXED)
    spec.repair.verify_with = "factory-journey"
    monkeypatch.setattr("hsf.harness.cf.shutil.which", lambda tool: None)
    receipt = heal(app, spec)
    assert receipt["rounds"][1]["action"] == "rollback:verifier_unavailable"


def test_factory_proofs_plan_reuses_without_running(tmp_path, monkeypatch):
    from hsf.harness.cf import plan_gates
    counter = tmp_path / "ran"
    gate = GateSpec(id="unit", kind="unit", flake_retries=0, inputs=["src/**"], read_only=True,
                    run=PY + f" -c \"open(r'{counter}', 'a').write('x')\"")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    log = _fake_factory(tmp_path, monkeypatch, (
        "disp = 'REUSE' if args[:2] == ['proofs', 'plan'] else 'RUN'\n"
        "print(json.dumps({'items': [{'gate': 'harness:unit', 'disposition': disp}]}))"))
    from hsf.harness.spec import HarnessSpec
    spec = HarnessSpec(harness="h", gates=[gate], mutable=["src/**"], cache="factory-proofs")
    runs = run_gates(spec.gates, tmp_path, planner=lambda: plan_gates(tmp_path, spec, {"src/a.py": "d"}))
    assert runs[0].classification == "reused" and not counter.exists()
    assert json.loads(log.read_text().splitlines()[0])[:2] == ["proofs", "plan"]
    monkeypatch.setattr("hsf.harness.cf.shutil.which", lambda tool: None)
    runs = run_gates(spec.gates, tmp_path, planner=lambda: plan_gates(tmp_path, spec, {}))
    assert runs[0].classification == "ok" and counter.exists()


def test_seed_from_specline_and_forgeline_is_idempotent_and_protected(tmp_path, capsys):
    app = _app(tmp_path, source=FIXED)
    (app / "plans").mkdir()
    (app / "plans" / "shop.md").write_text(
        "- [ ] T1 | slice=api | files=src/calc.py | verify=`python -c pass` | add numbers\n"
        "- [ ] T2 | slice=api | files=src/calc.py | verify=`python -c pass` | subtract numbers\n")
    (app / "smoke").mkdir()
    (app / "smoke" / "shop.json").write_text(json.dumps({"checks": [{"name": "adds two numbers"}]}))
    root = ["--root", str(app)]
    main(["harness", "seed", "--feature", "shop", "--json", *root])
    first = json.loads(capsys.readouterr().out)
    assert first["gates_added"] == ["sl-shop-t1", "sl-shop-t2", "forge-smoke-shop"]
    assert first["features_added"] == ["shop.T1", "shop.T2", "shop.smoke.adds-two-numbers"]
    main(["harness", "seed", "--feature", "shop", "--json", *root])
    assert json.loads(capsys.readouterr().out)["gates_added"] == []
    spec = load_harness(app)
    assert "plans/**" in spec.protected and protected_drift(app, spec) == []
    assert not any(f.passes for f in Ledger(app / spec.features_path).verify())


def test_proof_manifest_declares_a_gate_result_output_and_record_writes_it(tmp_path):
    """factory proofs record refuses requests without outputs; each gate declares its result file."""
    from hsf.harness import cf

    class Gate:
        id, run, inputs, read_only, safe_to_skip, relevant_paths = "unit", "pytest -q", ["src/**"], True, False, []

    class Spec:
        gates = [Gate()]

    manifest = cf.proof_manifest(tmp_path, Spec(), {"src/a.py": "x"})
    assert manifest["gates"][0]["outputs"] == [".hsf-harness/gates/unit.json"]
    calls = []
    ok = cf.record_gate(tmp_path, "unit", 120, run=lambda *a, **k: calls.append(a) or None, which=lambda _n: None,
                        summary={"gate": "unit", "passed": True, "classification": "ok", "output_tail": "secret"})
    written = (tmp_path / ".hsf-harness" / "gates" / "unit.json").read_text(encoding="utf-8")
    assert '"passed": true' in written and "secret" not in written and ok is False
