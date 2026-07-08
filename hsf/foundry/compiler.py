"""Foundry: compile a validated SpecModel into a Python artifact.

Engines:
  template — deterministic template-fill (no LLM, works offline). This is
             Compiled AI in its purest form: the spec IS the program.
  llm      — single model invocation per attempt (PRD R6.3.1), constrained
             to the same template slots; output re-validated identically.
Both paths produce byte-stable artifacts and pass the same four gates.
"""
from __future__ import annotations
import datetime as _dt
import secrets
from pathlib import Path
from jinja2 import Environment, StrictUndefined
from hsf.spec.models import SpecModel, FieldSpec

TEMPLATE_VERSION = "wf-1"

class CompileError(RuntimeError):
    def __init__(self, msg: str, evidence: list | None = None):
        super().__init__(msg)
        self.evidence = evidence or []

def _class_name(spec_id: str) -> str:
    return "".join(w.capitalize() for w in spec_id.split("_")) + "Workflow"

def _bounds_guards(spec: SpecModel) -> tuple[list[dict], str]:
    guards, policy = [], "human_review"
    for step in spec.steps:
        if step.type != "bounded_invocation":
            continue
        policy = step.on_out_of_bounds or policy
        for field, fs in (step.schema_ or {}).items():
            if not isinstance(fs, FieldSpec):
                continue
            conds = []
            if fs.min is not None:
                conds.append(f"extracted['{field}'] > {fs.min}")
            if fs.max is not None:
                conds.append(f"extracted['{field}'] < {fs.max}")
            if conds:
                guards.append({
                    "expr": " and ".join(conds),
                    "message": f"Suspicious {field}",
                    "trace": f"{spec.workflow_spec}.yaml#{step.id}.{field}",
                })
    return guards, policy

def _decision_rules(spec: SpecModel) -> list[dict]:
    rules = []
    for step in spec.steps:
        if step.type != "branch":
            continue
        for i, r in enumerate(step.rules or []):
            trace = f"{spec.workflow_spec}.yaml#{step.id}.rules[{i}]"
            if r.if_ is not None and r.then is not None:
                cond = (r.if_.replace(" == true", " is True").replace(" == false", " is False"))
                cond_py = _fieldify(cond, spec)
                rules.append({"cond": cond_py, "status": r.then["status"], "reason": r.then["reason"], "trace": trace})
            elif r.else_ is not None:
                rules.append({"cond": None, "status": r.else_["status"], "reason": r.else_["reason"], "trace": trace})
    return rules

def _fieldify(cond: str, spec: SpecModel) -> str:
    import re
    fields = set()
    for step in spec.steps:
        if step.schema_:
            fields |= set(step.schema_.keys())
    def repl(m):
        name = m.group(0)
        return f"extracted['{name}']" if name in fields else name
    return re.sub(r"[A-Za-z_][A-Za-z0-9_]*", repl, cond)

def _extract_schema_literal(spec: SpecModel) -> str:
    out = {}
    for step in spec.steps:
        if step.type == "bounded_invocation" and step.schema_:
            for k, fs in step.schema_.items():
                d = {"type": fs.type}
                if fs.min is not None: d["min"] = fs.min
                if fs.max is not None: d["max"] = fs.max
                out[k] = d
    return repr(out)

def render_artifact(spec: SpecModel, spec_sha: str, engine: str, compiled_at: str | None = None) -> str:
    tpl_path = Path(__file__).resolve().parents[1] / "context" / "library" / "templates" / "workflow.py.j2"
    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    tpl = env.from_string(tpl_path.read_text())
    guards, policy = _bounds_guards(spec)
    return tpl.render(
        spec_id=spec.workflow_spec, version=spec.version, spec_sha=spec_sha,
        engine=engine, template_version=TEMPLATE_VERSION,
        compiled_at=compiled_at or "1970-01-01T00:00:00Z",  # pinned for byte-stability; real ts in receipt
        class_name=_class_name(spec.workflow_spec),
        extract_schema=_extract_schema_literal(spec),
        bounds_guards=guards, oob_policy=policy,
        decision_rules=_decision_rules(spec),
    )

def compile_spec(spec: SpecModel, spec_sha: str, engine: str = "template") -> tuple[str, dict]:
    """Returns (artifact_source, compile_meta). One LLM call max per attempt."""
    canary = secrets.token_urlsafe(32)
    meta = {"engine": engine, "canary": canary, "template_version": TEMPLATE_VERSION,
            "compiled_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    # LSP-style closed-world check happens for activity steps regardless of engine
    from hsf.context.lsp_resolver import resolve_signatures
    acts = [s.activity for s in spec.steps if s.type == "activity" and s.activity]
    meta["resolved_signatures"] = resolve_signatures(acts)

    if engine == "template":
        return render_artifact(spec, spec_sha, engine), meta
    if engine == "llm":
        from .llm_client import complete
        system = (Path(__file__).parent / "prompts" / "system_v1.txt").read_text().format(canary=canary)
        skeleton = render_artifact(spec, spec_sha, engine)
        out = complete(system, f"SPEC SHA {spec_sha}\nTEMPLATE (authoritative structure):\n{skeleton}")
        src = out.strip()
        if src.startswith("```"):
            src = src.strip("`\n")
            src = src.split("\n", 1)[1] if src.startswith("python") else src
        if canary in src:
            raise CompileError("canary leak in artifact (Gate 1 precondition)")
        if "AUTO-GENERATED by HSF Foundry" not in src:
            raise CompileError("missing artifact header block")
        return src, meta
    raise CompileError(f"unknown engine {engine!r}")
