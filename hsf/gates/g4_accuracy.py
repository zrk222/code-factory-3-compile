"""Gate 4 — Accuracy: full golden dataset vs compiled decision logic (mocked extractor)."""
from __future__ import annotations
from collections import defaultdict
from .base import GateResult, Finding
from hsf.attribution import Attribution, FailureClass, UnitResult
from .g3_execution import run as _exec_run

def run(source: str, golden_cases: list[dict]) -> GateResult:
    res = _exec_run(source, golden_cases, repeats=1, capture_outputs=True)
    if not res.passed:
        evidence = dict(res.evidence)
        attr = Attribution("accuracy", 1, 0, [
            UnitResult(
                "goldens:<execution>",
                "accuracy",
                False,
                "compiled artifact could not execute golden fixtures: " +
                " | ".join(f.message for f in res.findings),
                FailureClass.RUNTIME_CRASH,
            )
        ])
        evidence["attribution"] = attr.to_dict()
        evidence["by_category"] = {}
        evidence["first_divergence"] = None
        return GateResult("accuracy", False, res.findings, evidence)
    outs = res.evidence["outputs"]
    findings, correct = [], 0
    units = []
    categories: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "correct": 0})
    divergences = []
    for case, out in zip(golden_cases, outs):
        exp = case["expected"]
        case_id = str(case["case_id"])
        category = case.get("category", "uncategorized")
        categories[category]["n"] += 1
        passed = out["status"] == exp["status"] and out["reason"] == exp["reason"]
        if passed:
            correct += 1
            categories[category]["correct"] += 1
            case_evidence = "output matched expected status and reason"
        else:
            case_evidence = f"expected={exp!r}, got={out!r}"
            findings.append(Finding("HSF-ACC-001", "high",
                f"case {case_id}: got {out}, expected {exp}"))
            divergences.append({"case_id": case_id, "expected": exp, "got": out})
        units.append(UnitResult(
            unit=f"golden:{case_id}",
            stage="accuracy",
            passed=passed,
            evidence=case_evidence,
            failure_class=None if passed else FailureClass.WRONG_OUTPUT,
        ))
    acc = correct / max(len(golden_cases), 1)
    attr = Attribution("accuracy", len(units), correct, units)
    by_category = {
        name: {
            **counts,
            "rate": counts["correct"] / counts["n"] if counts["n"] else 0.0,
        }
        for name, counts in sorted(categories.items())
    }
    first = min(divergences, key=lambda item: item["case_id"]) if divergences else None
    return GateResult("accuracy", acc == 1.0, findings,
                      {"accuracy": acc, "n": len(golden_cases), "correct": correct,
                       "by_category": by_category,
                       "first_divergence": first,
                       "attribution": attr.to_dict()})
