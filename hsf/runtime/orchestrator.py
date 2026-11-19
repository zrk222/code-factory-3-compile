"""Deterministic Orchestrator — responsibilities are EXACTLY four (PRD 6.6):
read steps, resolve references, capture outputs, propagate state.
Runs with no LLM credentials (extractor holds its own restricted key)."""
from __future__ import annotations
import ast, asyncio, hashlib, json, math, time
from hsf.registry import verified_artifact_bytes, verify_artifact
from .extractor import StructuredExtractor
from pathlib import Path
from .audit import AuditLog
from .sandwich import SafetySandwich
from .state import StateStore

class RunContext:
    def __init__(self, sandwich: SafetySandwich, audit: AuditLog, spec_id: str, oob_policy: str = "human_review"):
        self._sandwich = sandwich
        self._audit = audit
        self._spec_id = spec_id
        self._oob_policy = oob_policy

    async def sandwich(self, schema: dict, text: str):
        out = await self._sandwich.invoke(schema, text)
        self._audit.append(step="extraction", spec_id=self._spec_id,
                           ok=out is not None, injection_flags=self._sandwich.flags)
        return out

    async def out_of_bounds(self, message: str, trace: str = "", policy: str | None = None):
        policy = policy or self._oob_policy
        self._audit.append(step="out_of_bounds", spec_id=self._spec_id,
                           policy=policy, message=message, trace=trace)
        mod = self._artifact_ns
        if policy == "reject":
            return mod["AuthResult"]("DENIED", message)
        return mod["AuthResult"]("HUMAN_REVIEW", message)

def _require(ok, message="unsupported structured artifact"):
    if not ok:
        raise ValueError(message)


def _literal_assignment(node):
    _require(isinstance(node, ast.Assign) and len(node.targets) == 1)
    _require(isinstance(node.targets[0], ast.Name))
    ast.literal_eval(node.value)
    return node.targets[0].id


def _validate_result(node):
    expected = ast.parse("@dataclass(frozen=True)\nclass AuthResult:\n status: str\n reason: str").body[0]
    _require(ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False))


def _validate_workflow(node, schema):
    _require(node.name.endswith("Workflow") and not node.bases and not node.keywords and not node.decorator_list)
    _require(len(node.body) == 4)
    names = [_literal_assignment(n) for n in node.body[:3]]
    _require(names == ["SPEC_ID", "SPEC_SHA", "COMPILED_AT"])
    method = node.body[3]
    _require(isinstance(method, ast.AsyncFunctionDef) and method.name == "run")
    args = ast.parse("async def run(self, ctx, input_data): pass").body[0].args
    _require(ast.dump(method.args) == ast.dump(args) and not method.decorator_list and method.returns is None)
    _validate_body(method)
    _validate_prefix(method.body, schema)


def _validate_body(method):
    allowed = (ast.AsyncFunctionDef, ast.arguments, ast.arg, ast.Assign, ast.Name, ast.Load,
               ast.Store, ast.Await, ast.Call, ast.Attribute, ast.Subscript, ast.Constant,
               ast.If, ast.Return, ast.Compare, ast.Is, ast.IsNot, ast.Eq, ast.NotEq,
               ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.UnaryOp, ast.Not, ast.USub,
               ast.BoolOp, ast.And, ast.Or, ast.keyword)
    for n in ast.walk(method):
        _require(isinstance(n, allowed))
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            _require(n.id == "extracted")
        if isinstance(n, ast.Attribute):
            _require(isinstance(n.value, ast.Name) and n.value.id == "ctx" and n.attr in {"sandwich", "out_of_bounds"})
        if isinstance(n, ast.Call):
            _require(_allowed_call(n.func))
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store):
            _require(False)


def _same_ast(actual, expected):
    return ast.dump(actual) == ast.dump(expected)


def _schema_conditions(schema):
    _require(isinstance(schema, dict))
    conditions = []
    for field, fs in schema.items():
        _require(isinstance(field, str) and isinstance(fs, dict))
        _require(set(fs) <= {"type", "min", "max"} and fs.get("type") in {"boolean", "float", "int", "string"})
        parts = _field_bounds(field, fs)
        if parts:
            conditions.append((field, " and ".join(parts)))
    return conditions


def _field_bounds(field, fs):
    parts = []
    for key, op in (("min", ">"), ("max", "<")):
        if key in fs:
            value = fs[key]
            _require(type(value) in (int, float) and fs["type"] in {"int", "float"})
            _require(not isinstance(value, float) or math.isfinite(value))
            parts.append(f"extracted[{field!r}] {op} {value!r}")
    return parts


def _validate_prefix(body, schema):
    prefix = ast.parse('extracted = await ctx.sandwich(EXTRACT_SCHEMA, input_data["text"])\n'
                       'if extracted is None:\n return AuthResult("HUMAN_REVIEW", "Extraction failed")').body
    _require(len(body) >= 3 and all(_same_ast(a, b) for a, b in zip(body[:2], prefix)))
    conditions = _schema_conditions(schema)
    _require(len(body) >= 3 + len(conditions))
    policies = set()
    for node, (field, expression) in zip(body[2:], conditions):
        policies.add(_validate_guard(node, field, expression))
    _require(len(policies) <= 1)
    decisions = body[2 + len(conditions):]
    _require(isinstance(decisions[-1], ast.Return))
    for node in decisions:
        _validate_decision(node)


def _validate_guard(node, field, expression):
    _require(isinstance(node, ast.If) and len(node.body) == 1 and not node.orelse)
    expected = ast.parse(f"not ({expression})", mode="eval").body
    _require(_same_ast(node.test, expected))
    ret = node.body[0]
    _require(isinstance(ret, ast.Return) and isinstance(ret.value, ast.Await))
    call = ret.value.value
    _require(isinstance(call, ast.Call) and ast.unparse(call.func) == "ctx.out_of_bounds")
    _require(len(call.args) == 1 and _same_ast(call.args[0], ast.Constant(f"Suspicious {field}")))
    _require([kw.arg for kw in call.keywords] == ["trace", "policy"])
    trace, policy = [ast.literal_eval(kw.value) for kw in call.keywords]
    _require(isinstance(trace, str) and policy in {"human_review", "reject", "clamp"})
    return policy


def _validate_decision(node):
    if isinstance(node, ast.If):
        _require(not node.orelse and len(node.body) == 1)
        _require(not any(isinstance(n, (ast.Call, ast.Await, ast.Attribute)) for n in ast.walk(node.test)))
        _validate_decision(node.body[0])
        return
    _require(isinstance(node, ast.Return) and isinstance(node.value, ast.Call))
    call = node.value
    _require(isinstance(call.func, ast.Name) and call.func.id == "AuthResult")
    _require(len(call.args) == 2 and not call.keywords)
    status, reason = [ast.literal_eval(arg) for arg in call.args]
    _require(status in {"APPROVED", "DENIED", "HUMAN_REVIEW"} and isinstance(reason, str))


def _allowed_call(fn):
    if isinstance(fn, ast.Name):
        return fn.id == "AuthResult"
    return isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "ctx" and fn.attr in {"sandwich", "out_of_bounds"}


def _validate_structured(source):
    tree = ast.parse(source)
    _require(len(tree.body) == 5)
    imports = ast.parse("from __future__ import annotations\nfrom dataclasses import dataclass").body
    _require([ast.dump(n) for n in tree.body[:2]] == [ast.dump(n) for n in imports])
    _require(_literal_assignment(tree.body[2]) == "EXTRACT_SCHEMA")
    _require(isinstance(tree.body[3], ast.ClassDef) and isinstance(tree.body[4], ast.ClassDef))
    _validate_result(tree.body[3])
    _validate_workflow(tree.body[4], ast.literal_eval(tree.body[2].value))


def _security_passed(source):
    # Gates import runtime injection helpers; defer this reverse dependency.
    from hsf.gates.g1_security import run
    return run(source.decode("utf-8")).passed


def _verified_source(path, verify, trusted_public_key, allow_legacy_hmac):
    if verify is None or verify is verify_artifact:
        return verified_artifact_bytes(path, trusted_public_key=trusted_public_key,
                                       allow_legacy_hmac=allow_legacy_hmac)
    source = verify(path)
    _require(isinstance(source, bytes), "custom verifier must return verified bytes")
    return source


class Orchestrator:
    def __init__(self, artifact_path: Path, extractor=None, audit_path: Path | None = None,
                 verify=None, oob_policy: str = "human_review", *, trusted_public_key=None,
                 expected_sha=None, allow_legacy_hmac=True, structured_only=False):
        self.artifact_path = Path(artifact_path)
        self.structured_only = structured_only
        if structured_only:
            _require(extractor is None, "structured-only rejects custom extractors")
            _require(verify is None or verify is verify_artifact, "structured-only rejects custom verifiers")
            _require(trusted_public_key is not None, "structured-only requires a pinned public key")
            allow_legacy_hmac = False
        source = _verified_source(self.artifact_path, verify, trusted_public_key, allow_legacy_hmac)
        _require(expected_sha is None or hashlib.sha256(source).hexdigest() == expected_sha,
                 "artifact digest mismatch")
        _require(_security_passed(source), "artifact security gate failed")
        if structured_only:
            _validate_structured(source)
        self.ns: dict = {}
        exec(compile(source, str(self.artifact_path), "exec"), self.ns)
        self.cls = next(v for k, v in self.ns.items() if k.endswith("Workflow") and isinstance(v, type))
        self.spec_id = getattr(self.cls, "SPEC_ID", "unknown")
        self.audit = AuditLog(audit_path or Path("audit") / f"{self.spec_id}.jsonl")
        self.extractor = extractor
        self.state = StateStore()
        self.oob_policy = oob_policy

    async def run_async(self, input_data: dict):
        t0 = time.perf_counter()
        extractor = StructuredExtractor(input_data.get("fields")) if self.structured_only else self.extractor
        payload = {"text": "", "fields": input_data.get("fields")} if self.structured_only else input_data
        ctx = RunContext(SafetySandwich(extractor), self.audit, self.spec_id, self.oob_policy)
        ctx._artifact_ns = self.ns
        self.state.set("input_data", payload)
        result = await self.cls().run(ctx, payload)
        out = {"status": result.status, "reason": result.reason}
        h = hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()
        self.state.set("result", out)
        self.audit.append(step="result", spec_id=self.spec_id, output=out,
                          output_sha=h, latency_ms=round((time.perf_counter()-t0)*1000, 3))
        return result

    def run(self, input_data: dict):
        return asyncio.run(self.run_async(input_data))

    async def run_structured_async(self, fields: dict):
        _require(self.structured_only, "run_structured requires structured-only profile")
        return await self.run_async({"text": "", "fields": fields})

    def run_structured(self, fields: dict):
        return asyncio.run(self.run_structured_async(fields))
