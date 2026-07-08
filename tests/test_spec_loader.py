import pytest, yaml
from pathlib import Path
from hsf.spec import load_spec, SpecError

ROOT = Path(__file__).resolve().parents[1]
BASE = yaml.safe_load((ROOT / "specs" / "glp1_review.yaml").read_text())

def _write(tmp_path, data):
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(data))
    return p

def test_reference_spec_loads(spec_and_sha):
    spec, sha = spec_and_sha
    assert spec.workflow_spec == "glp1_review" and len(sha) == 64

def test_rejects_unbound_compliance_tag(tmp_path):
    d = yaml.safe_load(yaml.safe_dump(BASE)); d["metadata"]["compliance"] = ["NOT_A_GUARD"]
    with pytest.raises(SpecError) as e: load_spec(_write(tmp_path, d))
    assert e.value.code == "E_COMPLIANCE_UNBOUND"

def test_rejects_nonexhaustive_branch(tmp_path):
    d = yaml.safe_load(yaml.safe_dump(BASE))
    d["steps"][1]["rules"] = [r for r in d["steps"][1]["rules"] if "else" not in r]
    with pytest.raises(SpecError) as e: load_spec(_write(tmp_path, d))
    assert e.value.code == "E_NONEXHAUSTIVE"

def test_static_reference_dag_rejects_cycles(tmp_path):
    d = yaml.safe_load(yaml.safe_dump(BASE))
    d["steps"].append({"id": "extract_clinical_factors", "type": "terminal"})
    with pytest.raises(SpecError) as e: load_spec(_write(tmp_path, d))
    assert e.value.code == "E_CYCLE"

def test_rejects_unknown_field_reference(tmp_path):
    d = yaml.safe_load(yaml.safe_dump(BASE))
    d["steps"][1]["rules"][0]["if"] = "nonexistent_field == true"
    with pytest.raises(SpecError) as e: load_spec(_write(tmp_path, d))
    assert e.value.code == "E_UNRESOLVED_REF"

def test_clamp_forbidden_with_compliance(tmp_path):
    d = yaml.safe_load(yaml.safe_dump(BASE)); d["steps"][0]["on_out_of_bounds"] = "clamp"
    with pytest.raises(SpecError) as e: load_spec(_write(tmp_path, d))
    assert e.value.code == "E_CLAMP_FORBIDDEN"
