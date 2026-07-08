"""Every gallery spec compiles and passes 100% goldens — zero code changes (the platform claim)."""
import json
from pathlib import Path
import pytest
from hsf.spec import load_spec
from hsf.foundry.compiler import compile_spec
from hsf.gates.g4_accuracy import run as g4

ROOT = Path(__file__).resolve().parents[1]
ALL_SPECS = sorted(p.stem for p in (ROOT / "specs").glob("*.yaml"))

@pytest.mark.parametrize("name", ALL_SPECS)
def test_spec_compiles_and_passes_goldens(name):
    spec, sha = load_spec(ROOT / "specs" / f"{name}.yaml")
    src, _ = compile_spec(spec, sha)
    goldens = [json.loads(l) for l in (ROOT / "goldens" / name / "cases.jsonl").read_text().splitlines()]
    r = g4(src, goldens)
    assert r.passed, r.findings
    assert r.evidence["accuracy"] == 1.0
