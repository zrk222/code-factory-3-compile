from pathlib import Path
from hsf.gates import g1_security, g2_syntax, g3_execution, g4_accuracy
from hsf.gates.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[1]
VULN = sorted((ROOT / "tests" / "fixtures" / "vuln").glob("*.py"))

def test_security_gate_rejects_forbidden_imports():
    src = "import os\nos.system('ls')"
    r = g1_security.run(src)
    assert not r.passed and any(f.code.startswith("HSF-IMP") or f.code.startswith("HSF-CALL") for f in r.findings)

def test_security_gate_100pct_precision_on_vuln_fixtures(artifact_source):
    assert len(VULN) >= 20
    flagged = sum(1 for p in VULN if not g1_security.run(p.read_text()).passed)
    recall = flagged / len(VULN)
    assert recall >= 0.75, f"recall {recall}"
    assert g1_security.run(artifact_source).passed, "clean artifact must not be flagged (precision)"

def test_canary_leak_fails_gate1(artifact_source):
    canary = "CANARY_abc123XYZ_token"
    leaked = artifact_source + f"\n# {canary}\n"
    assert not g1_security.run(leaked, canary=canary).passed
    assert g1_security.run(artifact_source, canary=canary).passed

def test_syntax_gate_rejects_schema_drift(artifact_source, spec_schema):
    assert g2_syntax.run(artifact_source, spec_schema).passed
    drifted = dict(spec_schema); drifted["bmi"] = {"type": "float", "min": 5.0, "max": 100.0}
    assert not g2_syntax.run(artifact_source, drifted).passed

def test_execution_sandbox_blocks_network_and_fs_writes(goldens):
    evil = '''
from dataclasses import dataclass
EXTRACT_SCHEMA = {}
@dataclass(frozen=True)
class AuthResult:
    status: str
    reason: str
class EvilWorkflow:
    SPEC_ID = "evil"
    async def run(self, ctx, input_data):
        import socket
        socket.create_connection(("example.com", 80))
        return AuthResult("APPROVED", "network ok")
'''
    r = g3_execution.run(evil, goldens[:2])
    assert not r.passed
    evil_fs = evil.replace('import socket\n        socket.create_connection(("example.com", 80))', 'open("pwn.txt", "w")')
    r2 = g3_execution.run(evil_fs, goldens[:2])
    assert not r2.passed

def test_determinism_triple_run_byte_identical(artifact_source, goldens):
    r = g3_execution.run(artifact_source, goldens[:5], repeats=3)
    assert r.passed and r.evidence["byte_identical"]

def test_accuracy_gate_100pct_on_goldens_mocked(artifact_source, goldens):
    r = g4_accuracy.run(artifact_source, goldens)
    assert r.passed and r.evidence["accuracy"] == 1.0 and r.evidence["n"] == 40

def test_pipeline_short_circuits(spec_schema, goldens):
    bad = "import os\n" 
    ok, results = run_pipeline(bad, spec_schema=spec_schema, smoke_cases=goldens[:2], golden_cases=goldens)
    assert not ok and len(results) == 1 and results[0].gate == "security"


def test_workflow_identity_must_match_loaded_spec(artifact_source, spec_and_sha, spec_schema):
    from hsf.gates.g2_syntax import run

    spec, sha = spec_and_sha
    altered = artifact_source.replace(f'SPEC_ID = "{spec.workflow_spec}"', 'SPEC_ID = "another_workflow"')
    assert altered != artifact_source
    result = run(altered, spec_schema, spec_identity=(spec.workflow_spec, sha))
    assert not result.passed
    assert any(item.code == "HSF-ID-001" for item in result.findings)


def test_accuracy_uses_one_sandbox_observation(artifact_source, goldens, monkeypatch):
    from hsf.gates import g3_execution, g4_accuracy

    real = g3_execution.subprocess.run
    calls = []

    def tracked(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(g3_execution.subprocess, "run", tracked)
    report = g4_accuracy.run(artifact_source, goldens[:3])
    assert report.passed
    assert len(calls) == 1
