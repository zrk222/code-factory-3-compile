import pytest
from hsf.foundry.regeneration import compile_with_regeneration
from hsf.foundry.compiler import CompileError
from hsf.gates.base import GateResult, Finding

def test_regeneration_feeds_traceback_and_stops_at_max(spec_and_sha, monkeypatch):
    spec, sha = spec_and_sha
    from hsf.foundry import regeneration
    seen_feedback = []
    def failing_runner(src):
        return False, [GateResult("syntax", False, [Finding("HSF-SYN-001", "critical", "boom", line=3)])]
    orig = regeneration.compile_spec
    def spy(spec, sha, engine="llm"):
        return orig(spec, sha, engine="template")  # avoid real LLM; template path
    monkeypatch.setattr(regeneration, "compile_spec", spy)
    with pytest.raises(CompileError) as e:
        compile_with_regeneration(spec, sha, failing_runner, engine="llm", max_attempts=3)
    assert len(e.value.evidence) == 3
    assert e.value.evidence[-1]["results"][0]["findings"][0]["message"] == "boom"

def test_template_engine_fails_fast_on_gate_failure(spec_and_sha):
    spec, sha = spec_and_sha
    def failing_runner(src):
        return False, [GateResult("accuracy", False, [Finding("HSF-ACC-001", "high", "mismatch")])]
    with pytest.raises(CompileError) as e:
        compile_with_regeneration(spec, sha, failing_runner, engine="template")
    assert len(e.value.evidence) == 1  # deterministic engine cannot self-vary
