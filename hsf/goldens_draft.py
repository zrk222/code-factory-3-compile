"""Draft golden *inputs* from a spec: one case per branch rule plus one out-of-bounds case.

Every draft row has `"expected": null`. A human fills in the expected decision and saves the reviewed file as
`cases.jsonl`; `hsf compile` never reads `cases.draft.jsonl`. Drafting inputs, never answers, means a draft
cannot become a tautological pass.
"""
from __future__ import annotations

import ast
from typing import Any

_DEFAULTS = {"boolean": False, "int": 0, "float": 0.0, "string": ""}


def _fields(spec) -> dict:
    out = {}
    for step in spec.steps:
        if step.type == "bounded_invocation" and step.schema_:
            out.update(step.schema_)
    return out


def _inside(field) -> object:
    """A value strictly inside the field's bounds (or its type default)."""
    if field.type not in ("int", "float"):
        return _DEFAULTS[field.type]
    low, high = field.min, field.max
    if low is not None and high is not None:
        mid = (low + high) / 2
        return int(mid) if field.type == "int" else round(float(mid), 4)
    if low is not None:
        return int(low) + 1 if field.type == "int" else float(low) + 1.0
    if high is not None:
        return int(high) - 1 if field.type == "int" else float(high) - 1.0
    return _DEFAULTS[field.type]


def _satisfy(op: ast.cmpop, value: Any, kind: str) -> Any:
    step = 1 if kind == "int" else 0.01
    if isinstance(op, (ast.Eq, ast.GtE, ast.LtE)) or isinstance(value, (bool, str)) or value is None:
        return value if not isinstance(op, ast.NotEq) else (not value if isinstance(value, bool) else None)
    if isinstance(op, ast.Gt):
        return value + step
    if isinstance(op, ast.Lt):
        return value - step
    return value


_MISSING = object()
_WORDS = {"true": True, "false": False, "null": None, "none": None}


def _literal(node: ast.AST) -> object:
    """A constant, or the spec language's bare `true` / `false` / `null`."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id.lower() in _WORDS:
        return _WORDS[node.id.lower()]
    return _MISSING


def _apply(node: ast.AST, values: dict, fields: dict) -> None:
    """Set field values so a condition built from and/or/compare is likely true (first branch of an `or`)."""
    if isinstance(node, ast.Expression):
        _apply(node.body, values, fields)
    elif isinstance(node, ast.BoolOp):
        for operand in (node.values if isinstance(node.op, ast.And) else node.values[:1]):
            _apply(operand, values, fields)
    elif (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and len(node.ops) == 1
          and node.left.id in fields and _literal(node.comparators[0]) is not _MISSING):
        chosen = _satisfy(node.ops[0], _literal(node.comparators[0]), fields[node.left.id].type)
        if chosen is not None:
            values[node.left.id] = chosen


def _out_of_bounds(fields: dict) -> dict | None:
    for name, field in fields.items():
        if field.max is not None:
            return {name: field.max + (1 if field.type == "int" else 1.0)}
        if field.min is not None:
            return {name: field.min - (1 if field.type == "int" else 1.0)}
    return None


def draft_cases(spec) -> list[dict]:
    fields = _fields(spec)
    base = {name: _inside(field) for name, field in fields.items()}
    rows = []
    rules = [rule for step in spec.steps if step.type == "branch" for rule in (step.rules or [])]
    for index, rule in enumerate(rules):
        values = dict(base)
        if rule.if_ is not None:
            _apply(ast.parse(rule.if_, mode="eval"), values, fields)
        label = f"if {rule.if_}" if rule.if_ is not None else "else"
        rows.append({"case_id": f"draft{index:02d}", "input_text": f"draft: {label}", "extracted": values,
                     "expected": None, "targets_rule": label})
    oob = _out_of_bounds(fields)
    if oob:
        rows.append({"case_id": f"draft{len(rules):02d}", "input_text": "draft: out of bounds",
                     "extracted": base | oob, "expected": None, "targets_rule": "bounds"})
    return rows
