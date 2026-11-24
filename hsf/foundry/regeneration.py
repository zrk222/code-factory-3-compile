"""Bounded recovery for gated harness compilation."""
from __future__ import annotations

from hashlib import sha256

from .compiler import CompileError, compile_spec

MAX_REGEN_ATTEMPTS = 2
REMEDIATION_POLICIES = ("template-fallback", "off")
_TEMPLATE_FALLBACK_GATES = frozenset({"syntax", "execution", "accuracy"})


def compile_with_regeneration(
    spec,
    spec_sha,
    gate_runner,
    engine="template",
    max_attempts=MAX_REGEN_ATTEMPTS,
    *,
    policy="template-fallback",
):
    """Validate every candidate and recover using the unchanged spec and goldens."""
    if policy not in REMEDIATION_POLICIES or type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("a known remediation policy and positive attempt limit are required")
    evidence = []
    current_engine = engine
    model_compile_meter = None
    for attempt in range(1, min(max_attempts, MAX_REGEN_ATTEMPTS) + 1):
        src, meta = compile_spec(spec, spec_sha, engine=current_engine)
        passed, results = gate_runner(src)
        failed_gate = next((result.gate for result in results if not result.passed), None)
        if passed != (bool(results) and failed_gate is None):
            raise CompileError("gate runner returned inconsistent evidence", evidence)
        row = {
            "attempt": attempt,
            "engine": current_engine,
            "artifact_sha256": sha256(src.encode("utf-8")).hexdigest(),
            "passed": passed,
            "results": [result.summary() for result in results],
        }
        evidence.append(row)
        if passed:
            row["action"] = "accept"
            meta["attempts"] = attempt
            meta["remediation"] = {"policy": policy, "attempts": evidence}
            if model_compile_meter is not None:
                meta["token_meter"]["compile"] = model_compile_meter
            return src, meta, evidence
        recover = (
            policy == "template-fallback"
            and current_engine == "llm"
            and failed_gate in _TEMPLATE_FALLBACK_GATES
            and attempt < min(max_attempts, MAX_REGEN_ATTEMPTS)
        )
        if not recover:
            row["action"] = "stop"
            break
        row["action"] = "rebuild_from_spec"
        model_compile_meter = meta.get("token_meter", {}).get("compile")
        current_engine = "template"
    raise CompileError(f"artifact failed gates after {len(evidence)} attempt(s)", evidence)
