"""LTAP factory gate: Ingest -> Decide -> Act -> Update -> Audit.

Maps the PRD's four validation gates into the Act stage; Ingest binds the
spec + boundary requirements, Decide fixes the module seam (engine + doctrine),
Update refreshes the receipt + doctrine hash, Audit verifies evidence before
a build may be called shipped. Order fixed, short-circuit on first failure.
"""
from __future__ import annotations
import datetime as _dt
import json
from pathlib import Path
from . import g1_security, g2_syntax, g3_execution, g4_accuracy
from .base import GateResult
from hsf.context.assembler import doctrine_hash

def run_pipeline(source: str, *, spec_schema: dict, smoke_cases: list[dict],
                 golden_cases: list[dict], canary: str | None = None,
                 input_texts: list[str] | None = None) -> tuple[bool, list[GateResult]]:
    results: list[GateResult] = []
    for gate_fn, kwargs in (
        (g1_security.run, {"canary": canary, "input_texts": input_texts}),
        (g2_syntax.run, {"spec_schema": spec_schema}),
        (g3_execution.run, {"smoke_cases": smoke_cases}),
        (g4_accuracy.run, {"golden_cases": golden_cases}),
    ):
        r = gate_fn(source, **kwargs)
        results.append(r)
        if not r.passed:
            return False, results
    return True, results

def write_receipt(out_dir: Path, *, spec_id: str, spec_sha: str, artifact_sha: str,
                  compile_meta: dict, results: list[GateResult], shipped: bool) -> Path:
    """LTAP Update+Audit: receipt-owned evidence; no hand-copied metrics."""
    out_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "ltap": ["ingest", "decide", "act", "update", "audit"],
        "spec_id": spec_id, "spec_sha": spec_sha, "artifact_sha": artifact_sha,
        "doctrine_hash": doctrine_hash(),
        "engine": compile_meta.get("engine"),
        "attempts": compile_meta.get("attempts", 1),
        "compiled_at": compile_meta.get("compiled_at"),
        "gates": [r.summary() | {"evidence": r.evidence} for r in results],
        "shipped": shipped,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    p = out_dir / f"{spec_id}-{artifact_sha[:12]}.receipt.json"
    p.write_text(json.dumps(receipt, indent=2, sort_keys=True))
    return p
