"""Bounded, guarded, receipted repair loop for an app harness.

Each round: verify the gates. On an environment failure, re-run `init` once. Otherwise snapshot the tree,
hand a failure report to the external repair command, and check the guard (protected files byte-identical,
every change inside a declared mutable path). Then re-verify. A repair is kept only if it passes the guard
and reduces the failing gates; otherwise it is rolled back. The loop stops when every required gate passes,
when a failure signature repeats (oscillation), when no repair command is configured, or after
`repair.max_attempts`. Every round is written to the heal receipt.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import guard
from .gates import failing, gate_env, run_gates
from .ledger import Ledger
from .spec import HarnessSpec

WORK_DIR = ".hsf-harness"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def append_progress(root: Path, spec: HarnessSpec, event: str, **fields) -> None:
    path = Path(root) / spec.progress_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": _now(), "event": event, **fields}, sort_keys=True) + "\n")


def run_init(root: Path, spec: HarnessSpec) -> list[dict]:
    results: list[dict[str, Any]] = []
    for command in spec.init:
        try:
            done = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True, errors="replace",
                                  timeout=spec.repair.timeout_s, env=gate_env())
        except subprocess.TimeoutExpired:
            results.append({"command": command, "exit_code": None, "output_tail": "init command timed out"})
            break
        results.append({"command": command, "exit_code": done.returncode,
                        "output_tail": (done.stdout + done.stderr)[-2000:]})
        if done.returncode != 0:
            break
    return results


def _failure_report(root: Path, spec: HarnessSpec, failures, attempt: int) -> Path:
    try:
        feature = Ledger(Path(root) / spec.features_path).next_feature()
    except (OSError, ValueError, KeyError):
        feature = None
    report = {
        "schema": "hsf.harness-failure.v1", "attempt": attempt, "harness": spec.harness,
        "failing_gates": [run.summary() for run in failures],
        "mutable": spec.mutable, "protected": spec.protected,
        "rules": ["Edit only files matching `mutable`.", "Never edit, delete or weaken tests, goldens or specs.",
                  "Fix the cause shown in failing_gates; do not change gate commands."],
        "next_feature": feature.model_dump() if feature else None,
    }
    path = Path(root) / WORK_DIR / "failure.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def _invoke_repair(root: Path, spec: HarnessSpec, report: Path) -> dict:
    assert spec.repair.command is not None
    env = dict(gate_env(), HSF_FAILURE_REPORT=str(report.resolve()), HSF_HARNESS_ROOT=str(Path(root).resolve()))
    try:
        done = subprocess.run(spec.repair.command, shell=True, cwd=root, env=env, capture_output=True, text=True,
                              errors="replace", timeout=spec.repair.timeout_s)
        return {"exit_code": done.returncode, "output_tail": (done.stdout + done.stderr)[-2000:]}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "output_tail": "repair command timed out"}


def _signatures(failures) -> frozenset[str]:
    return frozenset(run.signature for run in failures)


def _digests(root: Path, spec: HarnessSpec) -> dict[str, str]:
    return guard.tree_digests(root, spec.protected)


def _verify(root: Path, spec: HarnessSpec, *, use_cache: bool = True):
    runs = run_gates(spec.gates, root, parallel=spec.parallel_gates, use_cache=use_cache)
    return runs, failing(runs, spec.gates)


def _heal_environment(root: Path, spec: HarnessSpec, failures, rounds: list) -> tuple[list, list]:
    """Re-run init once when every failure is environmental; return fresh gate results."""
    if not (spec.repair.reinit_on_environment_failure and spec.init and failures
            and all(run.classification == "environment" for run in failures)):
        return [], failures
    init = run_init(root, spec)
    runs, failures = _verify(root, spec)
    rounds.append({"round": "reinit", "init": init, "gates": [run.summary() for run in runs],
                   "action": "keep" if not failures else "continue"})
    return runs, failures


def _rollback(row: dict[str, Any], snapshot: guard.Snapshot, root: Path, spec: HarnessSpec, action: str) -> None:
    row["rolled_back"] = snapshot.restore(_digests(root, spec))
    if snapshot.incomplete:
        row["rollback_incomplete"] = snapshot.incomplete
    row["action"] = action


def _repair_round(root: Path, spec: HarnessSpec, failures, attempt: int, seen: set) -> tuple[dict, list, str]:
    """One guarded repair attempt. Returns (receipt row, failures after, stop reason or "").

    A repair is kept only when the guard passes and the set of failing required gates strictly shrinks;
    verification after a repair never reads the gate cache.
    """
    store = Path(root) / WORK_DIR / f"snapshot-{attempt}"
    before = _digests(root, spec)
    snapshot = guard.Snapshot(root, store, before)
    row: dict[str, Any] = {"round": attempt, "classification": sorted({run.classification for run in failures})}
    try:
        row["repair"] = _invoke_repair(root, spec, _failure_report(root, spec, failures, attempt))
        verdict = guard.check(before, _digests(root, spec), protected=spec.protected, mutable=spec.mutable,
                              unobserved=spec.unobserved, root=root, store=store)
        row["guard"] = verdict.summary()
        if not verdict.ok:
            _rollback(row, snapshot, root, spec, "rollback:guard")
            return row, failures, ""
        runs, after = _verify(root, spec, use_cache=False)
        row["gates"] = [run.summary() for run in runs]
        before_gates, after_gates = {run.gate for run in failures}, {run.gate for run in after}
        if after_gates < before_gates:
            seen.add(_signatures(after))
            row["action"] = "keep"
            row["kept_changes"] = {path: {"before_sha256": before.get(path), "after_sha256": digest}
                                   for path, digest in _digests(root, spec).items()
                                   if path in verdict.changed}
            return row, after, ""
        _rollback(row, snapshot, root, spec, "rollback:no_improvement")
        signatures = _signatures(after)
        stop = "oscillation" if after and signatures != _signatures(failures) and signatures in seen else ""
        return row, failures, stop
    except BaseException:
        _rollback(row, snapshot, root, spec, "rollback:interrupted")
        raise
    finally:
        snapshot.discard()


def heal(root: Path, spec: HarnessSpec) -> dict:
    """Run the bounded repair loop and write a receipt. Returns the receipt."""
    root = Path(root)
    runs, failures = _verify(root, spec)
    rounds: list = [{"round": "verify", "gates": [run.summary() for run in runs]}]
    _, failures = _heal_environment(root, spec, failures, rounds)
    seen = {_signatures(failures)} if failures else set()
    stop = "healthy" if not failures else ("no_repair_command" if not spec.repair.command else "")
    attempt = 0
    while failures and not stop and attempt < spec.repair.max_attempts:
        attempt += 1
        row, failures, stop = _repair_round(root, spec, failures, attempt, seen)
        rounds.append(row)
    if not stop:
        stop = "healthy" if not failures else "max_attempts"
    receipt: dict[str, Any] = {"schema": "hsf.harness-heal.v1", "harness": spec.harness, "at": _now(),
               "status": "healthy" if not failures else "unhealthy", "stop_reason": stop,
               "repair_attempts": attempt, "remaining_failures": [run.gate for run in failures], "rounds": rounds}
    out = root / spec.receipts_dir / f"heal-{receipt['at'][:19].replace(':', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    receipt["path"] = str(out)
    append_progress(root, spec, "heal", status=receipt["status"], stop_reason=stop, attempts=attempt,
                    receipt=out.relative_to(root).as_posix())
    return receipt
