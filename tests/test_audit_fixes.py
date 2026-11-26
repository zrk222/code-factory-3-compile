"""Audit fixes: CLI error boundary, scaffold names, compile hints, escaped literals, status contract,
inclusive bounds, --root, goldens drafts and the README install line."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import hsf
from hsf.scaffold import init_workflow

REPO = Path(__file__).resolve().parents[1]


def _hsf(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run([sys.executable, "-m", "hsf.cli", *argv], cwd=cwd, env=env, capture_output=True, text=True)


def _project(tmp_path: Path, name: str = "flow", edit=None) -> Path:
    init_workflow(name, tmp_path)
    spec_path = tmp_path / "specs" / f"{name}.yaml"
    if edit:
        data = yaml.safe_load(spec_path.read_text())
        edit(data)
        spec_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return spec_path


def test_validate_reports_schema_errors_without_traceback(tmp_path):
    bad_name = _project(tmp_path, edit=lambda d: d.update(workflow_spec="Bad-Name"))
    done = _hsf("validate", str(bad_name), cwd=tmp_path)
    assert done.returncode == 2 and "Traceback" not in done.stderr
    assert done.stderr.startswith("hsf: E_SPEC_SCHEMA: workflow_spec:")
    unknown = tmp_path / "specs" / "flow.yaml"
    data = yaml.safe_load(unknown.read_text()); data["workflow_spec"] = "flow"; data["surprise"] = 1
    unknown.write_text(yaml.safe_dump(data))
    done = _hsf("validate", str(unknown), cwd=tmp_path)
    assert done.returncode == 2 and "Traceback" not in done.stderr and "E_SPEC_SCHEMA" in done.stderr


def test_init_rejects_unloadable_names_and_reruns_cleanly(tmp_path):
    done = _hsf("init", "My-Flow", cwd=tmp_path)
    assert done.returncode == 2 and "'my_flow'" in done.stderr and "Traceback" not in done.stderr
    assert _hsf("init", "my_flow", cwd=tmp_path).returncode == 0
    again = _hsf("init", "my_flow", cwd=tmp_path)
    assert again.returncode == 2 and "already exists" in again.stderr and "Traceback" not in again.stderr


def test_missing_file_hints_match_the_kind(tmp_path):
    spec = _hsf("validate", "specs/none.yaml", cwd=tmp_path)
    assert "hsf init" in spec.stderr
    art = _hsf("goldens", "registry_store/none-*.py", "none", cwd=tmp_path)
    assert "hsf compile" in art.stderr


def test_failed_compile_names_the_gate_without_json(tmp_path):
    spec = _project(tmp_path)
    golden = tmp_path / "goldens" / "flow" / "cases.jsonl"
    rows = [json.loads(line) for line in golden.read_text().splitlines()]
    rows[0]["expected"]["status"] = "DENIED"
    golden.write_text("".join(json.dumps(row) + "\n" for row in rows))
    done = _hsf("compile", str(spec), cwd=tmp_path)
    assert done.returncode == 2 and "gate accuracy failed" in done.stderr and "--json" in done.stderr


def test_quotes_and_backslashes_in_reasons_compile_and_pass_goldens(tmp_path):
    reason = 'Said "ok" \\ fine'

    def quote(data):
        data["steps"][1]["rules"][0]["then"]["reason"] = reason

    spec = _project(tmp_path, edit=quote)
    golden = tmp_path / "goldens" / "flow" / "cases.jsonl"
    rows = [json.loads(line) for line in golden.read_text().splitlines()]
    rows[0]["expected"]["reason"] = reason
    golden.write_text("".join(json.dumps(row) + "\n" for row in rows))
    done = _hsf("compile", str(spec), cwd=tmp_path)
    assert done.returncode == 0 and "shipped=True" in done.stdout, done.stderr
    from hsf.challenge import challenge_spec
    report = challenge_spec(spec, golden)
    assert report["passed"] and report["mutants_killed"] == report["mutants_total"]


def test_rule_status_must_be_declared_in_outputs(tmp_path):
    spec = _project(tmp_path, edit=lambda d: d["steps"][1]["rules"][0]["then"].update(status="ESCALATE"))
    done = _hsf("validate", str(spec), cwd=tmp_path)
    assert done.returncode == 2 and "E_STATUS_UNDECLARED" in done.stderr and "ESCALATE" in done.stderr


@pytest.mark.parametrize("inclusive,expected", [(True, "DENIED"), (False, "HUMAN_REVIEW")])
def test_inclusive_minimum_lets_the_bound_reach_the_rules(tmp_path, inclusive, expected):
    def bound(data):
        score = data["steps"][0]["schema"]["score"]
        if inclusive:
            score["min_inclusive"] = True

    spec = _project(tmp_path, edit=bound)
    golden = tmp_path / "goldens" / "flow" / "cases.jsonl"
    rows = [json.loads(line) for line in golden.read_text().splitlines()]
    rows.append({"case_id": "c04", "input_text": "zero", "extracted": {"condition_met": False, "score": 0.0},
                 "expected": {"status": expected,
                              "reason": "Below Threshold" if inclusive else "Suspicious score"}})
    golden.write_text("".join(json.dumps(row) + "\n" for row in rows))
    done = _hsf("compile", str(spec), cwd=tmp_path)
    assert done.returncode == 0 and "shipped=True" in done.stdout, done.stderr


def test_root_places_registry_receipts_and_goldens_under_the_root(tmp_path):
    project = tmp_path / "project"
    spec = _project(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = _hsf("compile", str(spec), "--root", str(project), cwd=elsewhere)
    assert done.returncode == 0, done.stderr
    assert list((project / "registry_store").glob("flow-*.py")) and list((project / "receipts").glob("flow-*"))
    assert not (elsewhere / "registry_store").exists() and not (elsewhere / "receipts").exists()


def test_goldens_draft_writes_inputs_only_and_is_never_compiled(tmp_path):
    spec = _project(tmp_path)
    (tmp_path / "goldens" / "flow" / "cases.jsonl").unlink()
    done = _hsf("goldens-draft", str(spec), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    draft = tmp_path / "goldens" / "flow" / "cases.draft.jsonl"
    rows = [json.loads(line) for line in draft.read_text().splitlines()]
    assert len(rows) == 4 and all(row["expected"] is None for row in rows)
    assert _hsf("compile", str(spec), cwd=tmp_path).returncode == 2


def test_readme_install_line_does_not_pin_a_stale_release():
    pins = re.findall(r"code-factory-3-compile==([0-9][\w.]*)", (REPO / "README.md").read_text(encoding="utf-8"))
    assert all(pin == hsf.__version__ for pin in pins)


def test_dev_extra_installs_what_the_suite_imports():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    dev = re.search(r'^dev = \[(.*)\]$', pyproject, re.M).group(1)
    assert "cryptography" in dev and "pytest" in dev


def test_malformed_golden_rows_get_a_shape_error_not_a_traceback(tmp_path):
    import json

    import pytest

    from hsf.cli import CliError, _goldens_for

    path = tmp_path / "goldens" / "w" / "cases.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"input": {"a": 1}, "expected": {"status": "APPROVED", "reason": "x"}}) + "\n",
                    encoding="utf-8")
    with pytest.raises(CliError, match="E_GOLDEN_SHAPE: .*:1: .*missing: input_text, extracted"):
        _goldens_for("w", tmp_path)
    path.write_text('{"input_text": "t", "extracted": {}, "expected": {"status": "APPROVED", "reason": "x"}}\nnot json\n',
                    encoding="utf-8")
    with pytest.raises(CliError, match=r"E_GOLDEN_SHAPE: .*:2: not JSON"):
        _goldens_for("w", tmp_path)
