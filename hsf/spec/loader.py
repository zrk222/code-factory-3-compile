"""YAML -> SpecModel with fail-fast structural rules (no LLM involved)."""
from __future__ import annotations
import ast, hashlib, json
from pathlib import Path
import yaml
from .models import SpecModel

class SpecError(ValueError):
    def __init__(self, code: str, msg: str):
        self.code = code
        super().__init__(f"{code}: {msg}")

_ALLOWED_EXPR_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Name, ast.Load, ast.Constant,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)

def registered_guards(contracts_dir: Path | None = None) -> set[str]:
    d = contracts_dir or Path(__file__).resolve().parents[1] / "context" / "library" / "contracts"
    tags: set[str] = set()
    for f in sorted(d.glob("*.yaml")):
        data = yaml.safe_load(f.read_text()) or {}
        for t in data.get("compliance_guards", []):
            tags.add(t["tag"] if isinstance(t, dict) else t)
    return tags

def validate_expression(expr: str, known_fields: set[str]) -> None:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise SpecError("E_BAD_EXPR", f"unparseable condition {expr!r}: {e}")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_EXPR_NODES):
            raise SpecError("E_BAD_EXPR", f"disallowed construct {type(node).__name__} in {expr!r}")
        if isinstance(node, ast.Name) and node.id not in known_fields | {"true", "false", "True", "False"}:
            raise SpecError("E_UNRESOLVED_REF", f"condition references unknown field {node.id!r}")

def spec_sha256(raw_yaml: str) -> str:
    data = yaml.safe_load(raw_yaml)
    canon = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()

def load_spec(path: str | Path) -> tuple[SpecModel, str]:
    path = Path(path)
    raw = path.read_text()
    data = yaml.safe_load(raw)
    spec = SpecModel(**data)
    sha = spec_sha256(raw)

    produced: set[str] = set(spec.inputs.keys())
    step_ids: set[str] = set()
    for step in spec.steps:
        if step.id in step_ids:
            raise SpecError("E_CYCLE", f"duplicate step id {step.id!r}")
        step_ids.add(step.id)
        if step.type == "bounded_invocation":
            if not step.schema_:
                raise SpecError("E_NO_SCHEMA", f"bounded_invocation {step.id!r} missing schema")
            if not step.on_out_of_bounds:
                raise SpecError("E_NO_OOB_POLICY", f"bounded_invocation {step.id!r} missing on_out_of_bounds")
            if step.on_out_of_bounds == "clamp" and spec.metadata.compliance:
                raise SpecError("E_CLAMP_FORBIDDEN", "clamp forbidden on compliance-tagged specs")
            produced |= set(step.schema_.keys())
        elif step.type == "activity":
            if not step.activity:
                raise SpecError("E_NO_ACTIVITY", f"activity step {step.id!r} missing activity name")
            for a in step.args or []:
                if a.split(".")[0] not in produced:
                    raise SpecError("E_UNRESOLVED_REF", f"step {step.id!r} references {a!r} before production")
            produced.add(step.id)
        elif step.type == "branch":
            if not step.rules:
                raise SpecError("E_NO_RULES", f"branch {step.id!r} has no rules")
            if not any(r.else_ is not None for r in step.rules):
                raise SpecError("E_NONEXHAUSTIVE", f"branch {step.id!r} lacks else rule")
            for r in step.rules:
                if r.if_ is not None:
                    validate_expression(r.if_, produced)

    guards = registered_guards()
    for tag in spec.metadata.compliance:
        if tag not in guards:
            raise SpecError("E_COMPLIANCE_UNBOUND", f"compliance tag {tag!r} has no registered guard")
    return spec, sha
