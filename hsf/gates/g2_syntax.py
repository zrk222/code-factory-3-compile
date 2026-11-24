"""Gate 2 — Parse candidate source and verify its declared schema and identity."""
from __future__ import annotations

import ast

from .base import Finding, GateResult


def _literals(nodes, names):
    values = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    if target.id in values:
                        raise ValueError(f"duplicate artifact declaration: {target.id}")
                    values[target.id] = ast.literal_eval(node.value)
    return values


def run(source: str, spec_schema: dict, spec_identity: tuple[str, str] | None = None) -> GateResult:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return GateResult("syntax", False, [
            Finding("HSF-SYN-001", "critical", str(error), line=error.lineno or 0)
        ])
    findings = []
    try:
        schema = _literals(tree.body, {"EXTRACT_SCHEMA"}).get("EXTRACT_SCHEMA")
        if schema is None:
            findings.append(Finding("HSF-SCH-001", "critical", "EXTRACT_SCHEMA missing from artifact"))
        elif schema != spec_schema:
            findings.append(Finding("HSF-SCH-002", "critical",
                                    f"schema drift: artifact={schema!r} spec={spec_schema!r}"))
        if spec_identity is not None:
            classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name.endswith("Workflow")]
            names = ("SPEC_ID", "SPEC_SHA")
            actual = _literals(classes[0].body, names) if len(classes) == 1 else {}
            if tuple(actual.get(name) for name in names) != spec_identity:
                findings.append(Finding("HSF-ID-001", "critical", "workflow identity differs from the loaded spec"))
    except (TypeError, ValueError) as error:
        findings.append(Finding("HSF-SYN-002", "critical", str(error)))
    return GateResult("syntax", not findings, findings, {"checker": "ast+deepcompare"})
