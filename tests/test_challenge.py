import json
from pathlib import Path

from hsf.challenge import challenge_spec


ROOT = Path(__file__).resolve().parents[1]


def test_decision_counterfactual_kills_every_rule_and_injection_override():
    payload = challenge_spec(
        ROOT / "specs" / "glp1_review.yaml",
        ROOT / "goldens" / "glp1_review" / "cases.jsonl",
    )
    assert payload["schema"] == "factory.challenge.v1"
    assert payload["passed"] is True
    assert payload["mutants_total"] > 1
    assert payload["mutants_killed"] == payload["mutants_total"]
    assert any(item["unit"] == "injection_override" for item in payload["mutations"])


def test_cli_challenge_writes_receipt(tmp_path):
    from hsf.cli import main
    out = tmp_path / "challenge.json"
    main(["challenge", str(ROOT / "specs" / "glp1_review.yaml"), "-o", str(out)])
    payload = json.loads(out.read_text())
    assert payload["brick"] == "hsf"
    assert payload["passed"] is True
