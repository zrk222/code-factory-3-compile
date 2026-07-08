import json
from pathlib import Path
from hsf.gates.pipeline import run_pipeline, write_receipt
from hsf.registry.artifacts import artifact_sha

def test_receipt_contains_doctrine_hash_and_gate_evidence(tmp_path, artifact_source, spec_schema, goldens, spec_and_sha):
    spec, sha = spec_and_sha
    ok, results = run_pipeline(artifact_source, spec_schema=spec_schema,
                               smoke_cases=goldens[:3], golden_cases=goldens)
    p = write_receipt(tmp_path, spec_id=spec.workflow_spec, spec_sha=sha,
                      artifact_sha=artifact_sha(artifact_source),
                      compile_meta={"engine": "template", "attempts": 1}, results=results, shipped=ok)
    r = json.loads(p.read_text())
    assert r["shipped"] is True
    assert len(r["doctrine_hash"]) == 64
    assert [g["gate"] for g in r["gates"]] == ["security", "syntax", "execution", "accuracy"]
    assert r["gates"][3]["evidence"]["accuracy"] == 1.0
    assert r["ltap"] == ["ingest", "decide", "act", "update", "audit"]
