"""Gate 4 — Accuracy: full golden dataset vs compiled decision logic (mocked extractor)."""
from __future__ import annotations
import json
from .base import GateResult, Finding
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
    for case, out in zip(golden_cases, outs):
        exp = case["expected"]
        if out["status"] == exp["status"] and out["reason"] == exp["reason"]:
            correct += 1
        else:
            findings.append(Finding("HSF-ACC-001", "high",
                f"case {case['case_id']}: got {out}, expected {exp}"))
    acc = correct / max(len(golden_cases), 1)
    return GateResult("accuracy", acc == 1.0, findings,
                      {"accuracy": acc, "n": len(golden_cases), "correct": correct})
