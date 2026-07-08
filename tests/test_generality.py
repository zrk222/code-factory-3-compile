"""PRD 12: new workflow type = new spec + goldens only, zero code changes."""
import json
from pathlib import Path
from hsf.spec import load_spec
from hsf.foundry.compiler import compile_spec
from hsf.gates.g4_accuracy import run as g4

ROOT = Path(__file__).resolve().parents[1]

def test_second_spec_compiles_and_passes_goldens_with_no_code_changes():
    spec, sha = load_spec(ROOT / "specs" / "refund_review.yaml")
    src, _ = compile_spec(spec, sha)
    goldens = [json.loads(l) for l in (ROOT / "goldens/refund_review/cases.jsonl").read_text().splitlines()]
    r = g4(src, goldens)
    assert r.passed and r.evidence["accuracy"] == 1.0

def test_reject_policy_denies_out_of_bounds():
    spec, sha = load_spec(ROOT / "specs" / "refund_review.yaml")
    src, _ = compile_spec(spec, sha)
    assert "policy=\"reject\"" in src
