import pytest

from hsf.foundry.compiler import CompileError
from hsf.foundry.regeneration import compile_with_regeneration
from hsf.gates.base import Finding, GateResult


def test_regeneration_rebuilds_from_same_spec_and_uses_fresh_gates(spec_and_sha, monkeypatch):
    spec, sha = spec_and_sha
    from hsf.foundry import regeneration

    engines = []
    original = regeneration.compile_spec

    def compile_candidate(candidate, digest, *, engine):
        assert candidate is spec and digest == sha
        engines.append(engine)
        source, meta = original(candidate, digest)
        if engine == "llm":
            source += "\n# first attempt"
            meta["token_meter"]["compile"]["model_calls"] = 1
        return source, meta

    def gates(source):
        if source.endswith("# first attempt"):
            return False, [GateResult("accuracy", False, [Finding("HSF-ACC-001", "high", "mismatch")])]
        return True, [GateResult("accuracy", True)]

    monkeypatch.setattr(regeneration, "compile_spec", compile_candidate)
    source, meta, evidence = compile_with_regeneration(spec, sha, gates, engine="llm")
    assert engines == ["llm", "template"]
    assert not source.endswith("# first attempt")
    assert [row["action"] for row in evidence] == ["rebuild_from_spec", "accept"]
    assert evidence[0]["artifact_sha256"] != evidence[1]["artifact_sha256"]
    assert meta["token_meter"]["compile"]["model_calls"] == 1


@pytest.mark.parametrize(("engine", "gate", "policy"), [
    ("llm", "security", "template-fallback"),
    ("llm", "accuracy", "off"),
    ("template", "accuracy", "template-fallback"),
])
def test_regeneration_stops_without_eligible_recovery(spec_and_sha, monkeypatch, engine, gate, policy):
    spec, sha = spec_and_sha
    from hsf.foundry import regeneration

    original = regeneration.compile_spec
    monkeypatch.setattr(regeneration, "compile_spec", lambda spec, sha, *, engine: original(spec, sha))
    def reject(_):
        return False, [GateResult(gate, False)]
    with pytest.raises(CompileError) as error:
        compile_with_regeneration(spec, sha, reject, engine=engine, policy=policy)
    assert len(error.value.evidence) == 1
    assert error.value.evidence[0]["action"] == "stop"
