import json
from pathlib import Path

import pytest


def test_aku_export_has_seven_parts_and_autonomy(spec_and_sha, tmp_path):
    from hsf.aku import export_aku, write_aku

    spec, sha = spec_and_sha
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "shipped": True,
                "gates": [
                    {"gate": "security", "passed": True},
                    {"gate": "syntax", "passed": True},
                    {"gate": "execution", "passed": True},
                    {"gate": "accuracy", "passed": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    aku = export_aku(spec, sha, receipt)
    out = write_aku(aku, tmp_path / "glp1.aku.json")
    data = json.loads(out.read_text(encoding="utf-8"))

    assert set(data) == {
        "intent",
        "procedure",
        "tools",
        "metadata",
        "governance",
        "continuations",
        "validators",
        "autonomy",
    }
    assert data["autonomy"] == "autonomous"
    assert data["governance"]["runtime_model_calls"] == 0
    assert "deterministic_branch_logic" in data["validators"]["invariant"]


def test_aku_validator_gate_blocks_false_autonomy(spec_and_sha, tmp_path):
    from hsf.aku import validate_aku

    spec, _ = spec_and_sha
    receipt = tmp_path / "thin_receipt.json"
    receipt.write_text(json.dumps({"shipped": True}), encoding="utf-8")

    result = validate_aku(spec, receipt, require_autonomous=True)
    assert result["passed"] is False
    assert result["gate"] == "aku_validators"
    assert any(f["code"] == "AKU_POST_GATES" for f in result["findings"])


def test_aku_cli_writes_file(tmp_path):
    from hsf.cli import main

    spec = Path(__file__).resolve().parents[1] / "specs" / "refund_review.yaml"
    out = tmp_path / "refund_review.aku.json"
    main(["aku", str(spec), "-o", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["intent"]["workflow_spec"] == "refund_review"
    assert data["autonomy"] == "supervised"


def test_topology_validation_accepts_acyclic_manifest(tmp_path):
    from hsf.aku import validate_topology

    manifest = tmp_path / "topology.yaml"
    manifest.write_text(
        """
nodes:
  - id: spec
  - id: forge
  - id: compile
edges:
  - from: spec
    to: forge
  - from: forge
    to: compile
""",
        encoding="utf-8",
    )
    assert validate_topology(manifest) == {"nodes": 3, "edges": 2, "valid": True}


def test_topology_validation_rejects_cycles(tmp_path):
    from hsf.aku import validate_topology

    manifest = tmp_path / "topology.yaml"
    manifest.write_text(
        """
nodes: [a, b]
edges:
  - from: a
    to: b
  - from: b
    to: a
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="E_TOPOLOGY_CYCLE"):
        validate_topology(manifest)
