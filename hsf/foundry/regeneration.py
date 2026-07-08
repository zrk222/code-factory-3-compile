"""Regeneration Loop: gate failure -> repair context -> fresh compile attempt."""
from __future__ import annotations
from .compiler import compile_spec, CompileError

MAX_REGEN_ATTEMPTS = 3

def compile_with_regeneration(spec, spec_sha, gate_runner, engine="template", max_attempts=MAX_REGEN_ATTEMPTS):
    """gate_runner(source) -> (passed: bool, results: list[GateResult])"""
    evidence_chain = []
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        src, meta = compile_spec(spec, spec_sha, engine=engine)
        if feedback and engine == "llm":
            # repair, do not redesign: feedback is appended to the compile prompt
            meta["repair_context"] = feedback[:4000]
        passed, results = gate_runner(src)
        evidence_chain.append({"attempt": attempt, "passed": passed,
                               "results": [r.summary() for r in results]})
        if passed:
            meta["attempts"] = attempt
            return src, meta, evidence_chain
        failing = next(r for r in results if not r.passed)
        feedback = f"GATE {failing.gate} FAILED:\n" + "\n".join(
            f"{f.code} {f.file}:{f.line} {f.message}" for f in failing.findings)
        if engine == "template":
            break  # deterministic engine cannot self-vary; fail fast with evidence
    raise CompileError(f"artifact failed gates after {len(evidence_chain)} attempt(s)", evidence_chain)
