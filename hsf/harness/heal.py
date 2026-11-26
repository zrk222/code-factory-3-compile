"""Bounded, guarded, receipted repair loop for an app harness.

Each round: verify the gates. On an environment failure, re-run `init` once. Otherwise snapshot the tree,
hand a failure report to the external repair command, and check the guard (protected files, the gate cache
and existing evidence unchanged; no symlinks; every other change inside a declared mutable path). Then
re-verify without the cache. A repair is kept only if the guard passes and the set of failing required gates
strictly shrinks, and, with `repair.verify_with: factory-journey`, only if `factory journey heal-verify`
accepts it. Otherwise it is rolled back. The loop stops when every required gate passes, on oscillation, when
no repair command is configured, or after `repair.max_attempts`. If the run ends unhealthy, the tree is
restored to its state before the heal (unless `keep_partial`). Every round is written to a v2 receipt, and the
progress log is a hash chain.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import subprocess
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import guard
from .gates import failing, gate_env, run_command, run_gates
from .ledger import Ledger
from .spec import HarnessSpec

WORK_DIR = ".hsf-harness"
RECEIPT_SCHEMA = "hsf.harness-heal.v2"
# HSF attribution classes for harness failures (consumed by FactoryLine's rollup).
ATTRIBUTION = {"timeout": "runtime_timeout", "environment": "runtime_crash", "build": "runtime_crash",
               "verification": "wrong_output"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _file_sha(path: Path) -> str | None:
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _last_line_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    lines = path.read_bytes().splitlines()
    return sha256(lines[-1]).hexdigest() if lines else None


def append_progress(root: Path, spec: HarnessSpec, event: str, **fields) -> None:
    """Append one JSON line carrying the SHA-256 of the previous line, so edited history is detectable."""
    path = Path(root) / spec.progress_path
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": _now(), "event": event, "prev_sha256": _last_line_sha(path), **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def verify_progress_chain(root: Path, spec: HarnessSpec) -> list[int]:
    """1-based line numbers whose `prev_sha256` does not match the line before them."""
    path = Path(root) / spec.progress_path
    if not path.is_file():
        return []
    broken, previous = [], None
    for number, raw in enumerate(path.read_bytes().splitlines(), 1):
        try:
            claimed = json.loads(raw).get("prev_sha256", "absent")
        except ValueError:
            broken.append(number)
            previous = sha256(raw).hexdigest()
            continue
        if claimed != "absent" and claimed != previous:
            broken.append(number)
        previous = sha256(raw).hexdigest()
    return broken


def run_init(root: Path, spec: HarnessSpec) -> list[dict]:
    results: list[dict[str, Any]] = []
    for command in spec.init:
        code, out, _ = run_command(command, Path(root), spec.repair.timeout_s, gate_env(spec.env_allow))
        results.append({"command": command, "exit_code": code,
                        "output_tail": out[-2000:] if code is not None else "init command timed out"})
        if code != 0:
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
    env = dict(gate_env(spec.env_allow), HSF_FAILURE_REPORT=str(report.resolve()),
               HSF_HARNESS_ROOT=str(Path(root).resolve()))
    code, out, _ = run_command(spec.repair.command, Path(root), spec.repair.timeout_s, env)
    return {"exit_code": code, "output_tail": out[-2000:] if code is not None else "repair command timed out"}


def _signatures(failures) -> frozenset[str]:
    return frozenset(run.signature for run in failures)


def _digests(root: Path, spec: HarnessSpec) -> dict[str, str]:
    return guard.tree_digests(root, spec.protected)


def _verify(root: Path, spec: HarnessSpec, *, use_cache: bool = True):
    env = gate_env(spec.env_allow)
    if use_cache and spec.cache == "factory-proofs":
        from .cf import planner_for, record_gate
        digests = _digests(root, spec)
        runs = run_gates(spec.gates, root, parallel=spec.parallel_gates, env=env,
                         planner=planner_for(root, spec, digests),
                         recorder=lambda gate, run: record_gate(root, gate.id, int(run.duration_s * 1000)))
    else:
        runs = run_gates(spec.gates, root, parallel=spec.parallel_gates, env=env,
                         use_cache=use_cache and spec.cache == "local")
    for run in runs:
        if run.classification == "flake":
            append_progress(root, spec, "flake", gate=run.gate, attempts=run.attempts)
    return runs, failing(runs, spec.gates)


def _heal_environment(root: Path, spec: HarnessSpec, failures, rounds: list) -> tuple[list, list]:
    """Re-run init once when every failure is environmental; return fresh gate results."""
    if not (spec.repair.reinit_on_environment_failure and spec.init and failures
            and all(run.classification == "environment" for run in failures)):
        return [], failures
    init = run_init(root, spec)
    runs, failures = _verify(root, spec, use_cache=False)
    rounds.append({"round": "reinit", "init": init, "gates": [run.summary() for run in runs],
                   "action": "keep" if not failures else "continue"})
    return runs, failures


def _rollback(row: dict[str, Any], snapshot: guard.Snapshot, root: Path, spec: HarnessSpec, action: str) -> None:
    row["rolled_back"] = snapshot.restore(_digests(root, spec))
    if snapshot.incomplete:
        row["rollback_incomplete"] = snapshot.incomplete
    row["action"] = action


def kept_patch(root: Path, store: Path, changed: list[str], out_dir: Path) -> tuple[Path, str]:
    """Unified diff of one kept round (pre-repair copy vs. current tree)."""
    chunks: list[str] = []
    for rel in changed:
        old, new = store / rel, Path(root) / rel
        before = old.read_text(encoding="utf-8", errors="replace").splitlines(True) if old.is_file() else []
        after = new.read_text(encoding="utf-8", errors="replace").splitlines(True) if new.is_file() else []
        chunks.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"repair-{uuid.uuid4().hex[:12]}.patch"
    path.write_text("".join(chunks), encoding="utf-8")
    return path, sha256(path.read_bytes()).hexdigest()


def _coverage(root: Path, spec: HarnessSpec, digests: dict[str, str]) -> list[str]:
    """Protected files and ledger features: removing or editing a test breaks heal-verify's subset rule."""
    items = [f"{path}@{digest}" for path, digest in digests.items() if guard.matches(path, spec.protected)]
    try:
        items += [f"feature:{feature.id}" for feature in Ledger(Path(root) / spec.features_path).load()]
    except (OSError, ValueError):
        pass
    return sorted(items)


def _heal_verify(root: Path, spec: HarnessSpec, row: dict, before: dict[str, str], store: Path,
                 changed: list[str], failed: list[str]) -> bool:
    from .cf import heal_verify, heal_verify_accepts, healing_input
    patch = kept_patch(root, store, changed, Path(root) / WORK_DIR)
    heal_id = f"{spec.harness.lower()}-{uuid.uuid4().hex[:12]}"
    source = healing_input(root, spec, heal_id, patch, changed, _coverage(root, spec, before),
                           _coverage(root, spec, _digests(root, spec)), failed, store)
    result = heal_verify(root, source)
    row["heal_verify"] = None if result is None else {
        "decision": result.get("decision"), "receipt": result.get("receipt") or result.get("receipt_path"),
        "receipt_sha256": result.get("receipt_sha256")}
    return heal_verify_accepts(result)


def _keep(row: dict[str, Any], root: Path, spec: HarnessSpec, before: dict[str, str], changed: list[str],
          after: list, seen: set) -> tuple[dict, list, str]:
    seen.add(_signatures(after))
    current = _digests(root, spec)
    row["action"] = "keep"
    row["kept_changes"] = {path: {"before_sha256": before.get(path), "after_sha256": current.get(path)}
                           for path in changed}
    return row, after, ""


def _repair_round(root: Path, spec: HarnessSpec, failures, attempt: int, seen: set) -> tuple[dict, list, str]:
    """One guarded repair attempt. Returns (receipt row, failures after, stop reason or "")."""
    store = Path(root) / WORK_DIR / f"snapshot-{attempt}"
    before = _digests(root, spec)
    snapshot = guard.Snapshot(root, store, before, keep_globs=(*spec.mutable, *spec.protected, *guard.EVIDENCE))
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
        if {run.gate for run in after} < {run.gate for run in failures}:
            if spec.repair.verify_with == "factory-journey" and not _heal_verify(
                    root, spec, row, before, store, verdict.changed, [run.gate for run in failures]):
                reason = "rollback:verifier_unavailable" if row.get("heal_verify") is None else "rollback:heal_verify"
                _rollback(row, snapshot, root, spec, reason)
                return row, failures, ""
            return _keep(row, root, spec, before, verdict.changed, after, seen)
        _rollback(row, snapshot, root, spec, "rollback:no_improvement")
        signatures = _signatures(after)
        stop = "oscillation" if after and signatures != _signatures(failures) and signatures in seen else ""
        return row, failures, stop
    except BaseException:
        _rollback(row, snapshot, root, spec, "rollback:interrupted")
        raise
    finally:
        snapshot.discard()


def _attribution(failures) -> dict:
    units = [{"unit": f"harness:{run.gate}", "passed": False, "failure_class": ATTRIBUTION.get(run.classification,
              "wrong_output")} for run in failures]
    return {"stage": "harness_heal", "n_checked": max(1, len(units)), "n_passed": 0 if units else 1, "units": units}


def _write_receipt(root: Path, spec: HarnessSpec, receipt: dict) -> Path:
    from hsf.provenance import provenance
    receipt.update(
        schema=RECEIPT_SCHEMA, id=f"heal-{receipt['at'][:19].replace(':', '')}-{uuid.uuid4().hex[:8]}",
        harness_sha256=_file_sha(Path(root) / "harness" / "harness.yaml"),
        protected_lock_sha256=_file_sha(Path(root) / "harness" / "protected.lock.json"),
        tool={key: value for key, value in provenance().items() if key in ("package", "version")},
        repair_command_sha256=sha256(spec.repair.command.encode("utf-8")).hexdigest() if spec.repair.command else None)
    out = Path(root) / spec.receipts_dir / f"{receipt['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return out


def heal(root: Path, spec: HarnessSpec, *, keep_partial: bool = False) -> dict:
    """Run the bounded repair loop and write a receipt. Returns the receipt."""
    root = Path(root)
    start = guard.Snapshot(root, root / WORK_DIR / "snapshot-start", _digests(root, spec),
                           keep_globs=(*spec.mutable, *spec.protected, *guard.EVIDENCE))
    try:
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
        reverted = start.restore(_digests(root, spec)) if failures and not keep_partial and attempt else []
    finally:
        start.discard()
    receipt: dict[str, Any] = {
        "harness": spec.harness, "at": _now(), "status": "healthy" if not failures else "unhealthy",
        "stop_reason": stop, "repair_attempts": attempt, "remaining_failures": [run.gate for run in failures],
        "reverted_to_start": reverted, "rounds": rounds, "attribution": _attribution(failures)}
    out = _write_receipt(root, spec, receipt)
    receipt["path"] = str(out)
    append_progress(root, spec, "heal", status=receipt["status"], stop_reason=stop, attempts=attempt,
                    receipt=out.relative_to(root).as_posix(), receipt_sha256=_file_sha(out))
    return receipt
