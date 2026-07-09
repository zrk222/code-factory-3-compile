"""Gate 4 — Accuracy: full golden dataset vs compiled decision logic (mocked extractor)."""
from __future__ import annotations
import json
from collections import defaultdict
from .base import GateResult, Finding
from hsf.attribution import Attribution, FailureClass, UnitResult
from .sandbox_env import minimal_env
from .g3_execution import run as _exec_run

def run(source: str, golden_cases: list[dict]) -> GateResult:
    res = _exec_run(source, golden_cases, repeats=1)
    if not res.passed:
        return GateResult("accuracy", False, res.findings, res.evidence)
    # re-run to capture outputs for comparison
    import subprocess, sys, tempfile
    from pathlib import Path
    from .g3_execution import RUNNER
    with tempfile.TemporaryDirectory() as td:
        art = Path(td) / "a.py"; art.write_text(source)
        fx = Path(td) / "f.json"; fx.write_text(json.dumps(golden_cases))
        rn = Path(td) / "r.py"; rn.write_text(RUNNER, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(rn), str(art), str(fx)],
                              capture_output=True, text=True, env=minimal_env(), cwd=td, timeout=60)
    outs = json.loads(proc.stdout)
    findings, correct = [], 0
    units = []
    categories = defaultdict(lambda: {"n": 0, "correct": 0})
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
            evidence = "output matched expected status and reason"
        else:
            evidence = f"expected={exp!r}, got={out!r}"
            findings.append(Finding("HSF-ACC-001", "high",
                f"case {case_id}: got {out}, expected {exp}"))
            divergences.append({"case_id": case_id, "expected": exp, "got": out})
        units.append(UnitResult(
            unit=f"golden:{case_id}",
            stage="accuracy",
            passed=passed,
            evidence=evidence,
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
