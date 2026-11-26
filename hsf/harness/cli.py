"""`hsf harness ...` subcommands."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Imports of the harness modules (and pydantic) are deferred to the command that needs them, so other
# `hsf` commands do not pay for them.


def _emit(payload: dict, as_json: bool, lines: list[str]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else "\n".join(lines))


def cmd_init(args) -> int:
    from .scaffold import init_harness
    for path in init_harness(args.name, Path(args.root), target=args.target, with_cf=not args.no_cf):
        print(f"created {path}")
    print("next: edit harness/harness.yaml (gates, mutable paths, repair.command), run "
          "`hsf harness seal --reason \"configure gates\"`, then `hsf harness status`")
    return 0


def status_payload(root: Path) -> dict:
    from .ledger import Ledger
    from .scaffold import protected_drift
    from .spec import load_harness
    spec = load_harness(root)
    ledger = Ledger(root / spec.features_path)
    payload: dict[str, Any] = {"cwd": str(root.resolve()), "harness": spec.harness, "target": spec.target}
    try:
        features = ledger.verify()
        nxt = ledger.next_feature()
        payload.update(ledger_ok=True, features=len(features), passing=sum(f.passes for f in features),
                       next_feature=nxt.model_dump() if nxt else None)
    except (OSError, ValueError) as exc:
        payload.update(ledger_ok=False, ledger_error=str(exc))
    drift = protected_drift(root, spec)
    payload.update(protected_ok=not drift, protected_drift=drift)
    progress = root / spec.progress_path
    tail = progress.read_text(encoding="utf-8").splitlines()[-5:] if progress.exists() else []
    payload["recent_progress"] = [json.loads(line) for line in tail if line.strip()]
    return payload


def cmd_status(args) -> int:
    payload = status_payload(Path(args.root))
    nxt = payload.get("next_feature")
    lines = [f"cwd: {payload['cwd']}", f"harness: {payload['harness']} ({payload['target']})",
             f"ledger: {'ok' if payload['ledger_ok'] else 'TAMPERED: ' + payload.get('ledger_error', '')}"
             + (f", {payload['passing']}/{payload['features']} passing" if payload['ledger_ok'] else ""),
             f"protected files: {'ok' if payload['protected_ok'] else 'CHANGED: ' + ', '.join(payload['protected_drift'])}",
             f"next feature: {nxt['id'] + ' - ' + nxt['description'] if nxt else 'none (all passing)'}"]
    _emit(payload, args.json, lines)
    return 0 if payload["ledger_ok"] and payload["protected_ok"] else 3


def _seals_broken(root: Path, spec) -> list[str]:
    """Problems with the ledger seal or protected-file seal; empty when both hold."""
    from .ledger import Ledger
    from .scaffold import protected_drift
    problems = [f"protected file changed: {path}" for path in protected_drift(root, spec)]
    try:
        Ledger(root / spec.features_path).verify()
    except (OSError, ValueError) as exc:
        problems.append(f"ledger: {exc}")
    return problems


def cmd_check(args) -> int:
    from .gates import failing, run_gates
    from .spec import load_harness
    root = Path(args.root)
    spec = load_harness(root)
    broken = _seals_broken(root, spec)
    if broken and not args.allow_drift:
        print("refusing to run gates: " + "; ".join(broken) + " (use --allow-drift to run anyway)", file=sys.stderr)
        return 3
    args.no_cache = args.no_cache or bool(os.environ.get("CI"))
    runs = run_gates(spec.gates, root, only=args.gate or None, parallel=spec.parallel_gates,
                     use_cache=not args.no_cache)
    bad = failing(runs, spec.gates)
    payload = {"passed": not bad, "gates": [run.summary() for run in runs]}
    lines = [f"{'PASS' if r.passed else 'FAIL'} {r.gate} [{r.classification}] {r.duration_s:.1f}s" for r in runs]
    _emit(payload, args.json, lines)
    return 0 if not bad else 1


def cmd_heal(args) -> int:
    from .heal import heal
    from .scaffold import protected_drift
    from .spec import load_harness
    root = Path(args.root)
    spec = load_harness(root)
    if args.repair_command:
        spec.repair.command = args.repair_command
    if args.max_attempts:
        spec.repair = spec.repair.model_copy(update={"max_attempts": args.max_attempts})
        spec.repair.model_validate(spec.repair.model_dump())
    drift = protected_drift(root, spec)
    if drift:
        print(f"refusing to heal: protected files changed since sealing: {', '.join(drift)}", file=sys.stderr)
        return 3
    receipt = heal(root, spec)
    lines = [f"status: {receipt['status']} ({receipt['stop_reason']}) after {receipt['repair_attempts']} repair(s)",
             f"receipt: {receipt['path']}"]
    _emit(receipt, args.json, lines)
    return 0 if receipt["status"] == "healthy" else 1


def cmd_pass(args) -> int:
    from .gates import run_gates
    from .heal import append_progress
    from .ledger import Ledger
    from .scaffold import ledger_paths, protected_drift, seal_protected
    from .spec import load_harness
    root = Path(args.root)
    spec = load_harness(root)
    drift = protected_drift(root, spec, ignore=ledger_paths(spec))
    if drift:
        print(f"refusing to mark: protected files changed since sealing: {', '.join(drift)}", file=sys.stderr)
        return 3
    ledger = Ledger(root / spec.features_path)
    feature = next((f for f in ledger.verify() if f.id == args.feature), None)
    if feature is None:
        print(f"unknown feature {args.feature!r}", file=sys.stderr)
        return 2
    runs = run_gates(spec.gates, root, only=feature.gates, use_cache=False)
    missing = sorted(set(feature.gates) - {run.gate for run in runs})
    bad = [run for run in runs if not run.passed or run.classification != "ok"]
    if missing or bad:
        print(f"{feature.id} not marked: failing {[r.gate for r in bad]} missing {missing}", file=sys.stderr)
        append_progress(root, spec, "pass_refused", feature=feature.id, failing=[r.gate for r in bad])
        return 1
    ledger.set_passes(feature.id, True)
    seal_protected(root, spec, only=ledger_paths(spec))
    append_progress(root, spec, "pass", feature=feature.id, gates=[r.summary()["gate"] for r in runs],
                    actor=os.environ.get("USER", "unknown"))
    print(f"{feature.id} passes ({', '.join(r.gate for r in runs)})")
    return 0


def cmd_add_feature(args) -> int:
    from .heal import append_progress
    from .ledger import Feature, Ledger
    from .scaffold import ledger_paths, protected_drift, seal_protected
    from .spec import load_harness
    root = Path(args.root)
    spec = load_harness(root)
    drift = protected_drift(root, spec, ignore=ledger_paths(spec))
    if drift:
        print(f"refusing to add: protected files changed since sealing: {', '.join(drift)}", file=sys.stderr)
        return 3
    for gate in args.gate:
        spec.gate(gate)
    feature = Feature(id=args.id, category=args.category, description=args.description, steps=args.step,
                      gates=args.gate, priority=args.priority)
    Ledger(root / spec.features_path).add(feature)
    seal_protected(root, spec, only=ledger_paths(spec))
    append_progress(root, spec, "add_feature", feature=feature.id)
    print(f"added {feature.id}")
    return 0


def cmd_seal(args) -> int:
    from .heal import append_progress
    from .ledger import Ledger
    from .scaffold import protected_drift, seal_protected
    from .spec import load_harness
    root = Path(args.root)
    spec = load_harness(root)
    drift = protected_drift(root, spec)
    Ledger(root / spec.features_path).seal(Ledger(root / spec.features_path).load())
    seal_protected(root, spec)
    append_progress(root, spec, "seal", reason=args.reason, files=drift,
                    actor=os.environ.get("USER", "unknown"))
    print(f"sealed {len(drift)} changed protected file(s): {', '.join(drift) or 'none'}")
    return 0


def register(sub) -> None:
    h = sub.add_parser("harness", help="self-healing app harness: ledger, gates, guarded repair loop")
    hs = h.add_subparsers(required=True, dest="harness_cmd")
    s = hs.add_parser("init"); s.add_argument("name")
    s.add_argument("--no-cf", action="store_true", help="do not add installed Code Factory gates (forge qa, factory scan)")
    s.add_argument("--target", default="generic", choices=["worker", "web", "mobile", "agent-ui", "generic"])
    s.set_defaults(fn=cmd_init)
    s = hs.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = hs.add_parser("check"); s.add_argument("--gate", action="append"); s.add_argument("--json", action="store_true")
    s.add_argument("--no-cache", action="store_true", help="re-run gates even when their inputs are unchanged")
    s.add_argument("--allow-drift", action="store_true", help="run gates even if protected files or the ledger changed")
    s.set_defaults(fn=cmd_check)
    s = hs.add_parser("heal"); s.add_argument("--repair-command"); s.add_argument("--max-attempts", type=int)
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_heal)
    s = hs.add_parser("pass"); s.add_argument("feature"); s.set_defaults(fn=cmd_pass)
    s = hs.add_parser("add-feature"); s.add_argument("id"); s.add_argument("--description", required=True)
    s.add_argument("--step", action="append", required=True); s.add_argument("--gate", action="append", required=True)
    s.add_argument("--category", default="functional"); s.add_argument("--priority", type=int, default=100)
    s.set_defaults(fn=cmd_add_feature)
    s = hs.add_parser("seal", help="owner-approved re-seal after editing protected files (logged)")
    s.add_argument("--reason", required=True); s.set_defaults(fn=cmd_seal)
    for parser in hs.choices.values():
        parser.add_argument("--root", default=".")
