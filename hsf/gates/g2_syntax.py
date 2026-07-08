"""Gate 2 — Syntax: ast.parse + schema-drift deep compare (mypy optional)."""
from __future__ import annotations
import ast
from .base import GateResult, Finding

def _artifact_schema(source: str) -> dict | None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "EXTRACT_SCHEMA":
                    return ast.literal_eval(node.value)
    return None

def run(source: str, spec_schema: dict) -> GateResult:
    findings = []
    try:
        ast.parse(source)
    except SyntaxError as e:
        return GateResult("syntax", False, [Finding("HSF-SYN-001", "critical", str(e), line=e.lineno or 0)])
    art = _artifact_schema(source)
    if art is None:
        findings.append(Finding("HSF-SCH-001", "critical", "EXTRACT_SCHEMA missing from artifact"))
    elif art != spec_schema:
        findings.append(Finding("HSF-SCH-002", "critical",
                        f"schema drift: artifact={art!r} spec={spec_schema!r}"))
    return GateResult("syntax", not findings, findings, {"checker": "ast+deepcompare"})
