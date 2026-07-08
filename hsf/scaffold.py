"""hsf init — 60 seconds from zero to a compiling workflow of your own."""
from __future__ import annotations
import json
from pathlib import Path

SPEC_TPL = """workflow_spec: {name}
version: 1
metadata:
  owner: you@example.com
  compliance: []
inputs:
  input_data:
    text: string
steps:
  - id: extract_facts
    type: bounded_invocation
    schema:
      condition_met: boolean
      score: {{type: float, min: 0.0, max: 100.0}}
    on_out_of_bounds: human_review
  - id: decide
    type: branch
    rules:
      - if: "condition_met == true"
        then: {{status: APPROVED, reason: "Condition Met"}}
      - if: "score >= 75.0"
        then: {{status: APPROVED, reason: "High Score"}}
      - else: {{status: DENIED, reason: "Below Threshold"}}
outputs:
  AuthResult: {{status: "enum[APPROVED, DENIED, HUMAN_REVIEW]", reason: string}}
"""

def init_workflow(name: str, root: Path = Path(".")) -> list[Path]:
    spec_path = root / "specs" / f"{name}.yaml"
    golden_dir = root / "goldens" / name
    if spec_path.exists():
        raise FileExistsError(f"{spec_path} already exists")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    golden_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(SPEC_TPL.format(name=name))
    cases = [
        {"case_id": "c00", "input_text": "example met", "extracted": {"condition_met": True, "score": 10.0}, "expected": {"status": "APPROVED", "reason": "Condition Met"}},
        {"case_id": "c01", "input_text": "high score", "extracted": {"condition_met": False, "score": 90.0}, "expected": {"status": "APPROVED", "reason": "High Score"}},
        {"case_id": "c02", "input_text": "low score", "extracted": {"condition_met": False, "score": 20.0}, "expected": {"status": "DENIED", "reason": "Below Threshold"}},
        {"case_id": "c03", "input_text": "out of bounds", "extracted": {"condition_met": False, "score": 250.0}, "expected": {"status": "HUMAN_REVIEW", "reason": "Suspicious score"}},
    ]
    gp = golden_dir / "cases.jsonl"
    gp.write_text("".join(json.dumps(c) + "\n" for c in cases))
    return [spec_path, gp]
