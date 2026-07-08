from pathlib import Path


def test_end_to_end_example_topology_is_valid():
    from hsf.aku import validate_topology

    root = Path(__file__).resolve().parents[1]
    assert validate_topology(root / "examples" / "end_to_end" / "topology.yaml") == {
        "nodes": 5,
        "edges": 4,
        "valid": True,
    }


def test_end_to_end_example_mentions_receipted_meter():
    root = Path(__file__).resolve().parents[1]
    text = (root / "examples" / "end_to_end" / "README.md").read_text(encoding="utf-8")
    assert "token_meter.context_modules" in text
    assert "--require-autonomous" in text
