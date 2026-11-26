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
    mode = "off" if args.no_cf else args.cf
    for path in init_harness(args.name, Path(args.root), target=args.target, cf_mode=mode):
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
    from .gates import gate_env
    runs = run_gates(spec.gates, root, only=args.gate or None, parallel=spec.parallel_gates,
                     use_cache=not args.no_cache and spec.cache == "local", env=gate_env(spec.env_allow))
    bad = failing(runs, spec.gates)
    regressed = _record_regressions(root, spec, runs) if not broken else []
    payload = {"passed": not bad, "gates": [run.summary() for run in runs], "regressed_features": regressed,
               "attribution": _check_attribution(runs)}
    lines = [f"{'PASS' if r.passed else 'FAIL'} {r.gate} [{r.classification}] {r.duration_s:.1f}s" for r in runs]
    lines += [f"REGRESSED {feature} (passes reset to false)" for feature in regressed]
    _emit(payload, args.json, lines)
    return 0 if not bad else 1


_ATTRIBUTION = {"timeout": "runtime_timeout", "environment": "runtime_crash", "build": "runtime_crash",
                "verification": "wrong_output"}


def _check_attribution(runs) -> dict:
    """FactoryLine-readable attribution: one unit per gate (3.2 a)."""
    units = [{"unit": f"harness:{run.gate}", "stage": "harness_check", "passed": run.passed,
              "evidence": run.classification,
              "failure_class": None if run.passed else _ATTRIBUTION.get(run.classification, "wrong_output")}
             for run in runs]
    return {"stage": "harness_check", "n_checked": len(units), "n_passed": sum(u["passed"] for u in units),
            "units": units}


def _record_regressions(root: Path, spec, runs) -> list[str]:
    """A passing feature whose gate now fails goes back to passes=false, logged (HH-12)."""
    from .heal import append_progress
    from .ledger import Ledger
    from .scaffold import ledger_paths, seal_protected
    failed = {run.gate for run in runs if not run.passed}
    ledger = Ledger(root / spec.features_path)
    regressed = [f.id for f in ledger.verify() if f.passes and failed & set(f.gates)]
    for feature_id in regressed:
        ledger.set_passes(feature_id, False)
        append_progress(root, spec, "regression", feature=feature_id, gates=sorted(failed))
    if regressed:
        seal_protected(root, spec, only=ledger_paths(spec))
    return regressed


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
    receipt = heal(root, spec, keep_partial=args.keep_partial)
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
    ledger = Ledger(root / spec.features_path)
    removed = _removed_features(root, spec)
    print(f"changed protected file(s): {', '.join(drift) or 'none'}")
    if removed:
        print(f"features removed from the ledger: {', '.join(removed)}")
    if not args.yes and not sys.stdin.isatty():
        print("refusing to seal without --yes when not on a terminal", file=sys.stderr)
        return 3
    if not args.yes and input("seal these changes? [y/N] ").strip().lower() != "y":
        return 3
    ledger.seal(ledger.load())
    seal_protected(root, spec)
    append_progress(root, spec, "seal", reason=args.reason, approver=args.approver, files=drift,
                    removed_features=removed)
    print(f"sealed {len(drift)} changed protected file(s)")
    return 0


def _removed_features(root: Path, spec) -> list[str]:
    """Features recorded by earlier progress events but no longer in the ledger."""
    from .ledger import Ledger
    progress = root / spec.progress_path
    known = {"F001"}
    if progress.exists():
        for line in progress.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") in ("add_feature", "pass") and event.get("feature"):
                known.add(event["feature"])
            known.update(event.get("seeded_features") or [])
    try:
        current = {f.id for f in Ledger(root / spec.features_path).load()}
    except (OSError, ValueError):
        return []
    return sorted(known - current)


def cmd_seed(args) -> int:
    """Seed gates and features from SpecLine plans and ForgeLine smoke manifests (3.2 e). Idempotent."""
    import yaml
    from .cf import SEED_PROTECTED, seed_from_forgeline, seed_from_specline
    from .heal import append_progress
    from .ledger import Ledger
    from .scaffold import ledger_paths, protected_drift, seal_protected
    from .spec import load_harness
    root = Path(args.root)
    spec = load_harness(root)
    drift = protected_drift(root, spec, ignore=ledger_paths(spec))
    if drift:
        print(f"refusing to seed: protected files changed since sealing: {', '.join(drift)}", file=sys.stderr)
        return 3
    gates, features = [], []
    for source, seeder in (("specline", seed_from_specline), ("forgeline", seed_from_forgeline)):
        if args.source in (source, "all"):
            new_gates, new_features = seeder(root, args.feature)
            gates += new_gates
            features += new_features
    existing_gates = {gate.id for gate in spec.gates}
    ledger = Ledger(root / spec.features_path)
    existing_features = {feature.id for feature in ledger.verify()}
    add_gates = [gate for gate in gates if gate.id not in existing_gates]
    add_features = [feature for feature in features if feature.id not in existing_features]
    payload = {"gates_added": [g.id for g in add_gates], "features_added": [f.id for f in add_features],
               "dry_run": args.dry_run}
    if not args.dry_run and (add_gates or add_features):
        spec_file = root / "harness" / "harness.yaml"
        data = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
        data["gates"] = data.get("gates", []) + [g.model_dump(exclude_defaults=True) | {"id": g.id, "run": g.run}
                                                 for g in add_gates]
        data["protected"] = sorted(set(data.get("protected") or spec.protected) | set(SEED_PROTECTED))
        spec_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        for feature in add_features:
            ledger.add(feature)
        seal_protected(root, load_harness(root))
        append_progress(root, spec, "seed", source=args.source, feature=args.feature,
                        seeded_gates=payload["gates_added"], seeded_features=payload["features_added"])
    _emit(payload, args.json, [f"added gate {g}" for g in payload["gates_added"]]
          + [f"added feature {f}" for f in payload["features_added"]] or ["nothing new to seed"])
    return 0


def register(sub) -> None:
    h = sub.add_parser("harness", help="self-healing app harness: ledger, gates, guarded repair loop")
    hs = h.add_subparsers(required=True, dest="harness_cmd")
    s = hs.add_parser("init"); s.add_argument("name")
    s.add_argument("--no-cf", action="store_true", help="same as --cf off")
    s.add_argument("--cf", default="auto", choices=["on", "off", "auto"],
                   help="Code Factory gates: on, off, or auto (add what is installed); the choice is recorded")
    s.add_argument("--target", default="generic",
                   choices=["worker", "web", "mobile", "agent-ui", "api", "cli", "mcp", "generic"])
    s.set_defaults(fn=cmd_init)
    s = hs.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = hs.add_parser("check"); s.add_argument("--gate", action="append"); s.add_argument("--json", action="store_true")
    s.add_argument("--no-cache", action="store_true", help="re-run gates even when their inputs are unchanged")
    s.add_argument("--allow-drift", action="store_true", help="run gates even if protected files or the ledger changed")
    s.set_defaults(fn=cmd_check)
    s = hs.add_parser("heal"); s.add_argument("--repair-command"); s.add_argument("--max-attempts", type=int)
    s.add_argument("--keep-partial", action="store_true",
                   help="keep improving repairs even if the run ends unhealthy (default: restore the start state)")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_heal)
    s = hs.add_parser("pass"); s.add_argument("feature"); s.set_defaults(fn=cmd_pass)
    s = hs.add_parser("add-feature"); s.add_argument("id"); s.add_argument("--description", required=True)
    s.add_argument("--step", action="append", required=True); s.add_argument("--gate", action="append", required=True)
    s.add_argument("--category", default="functional"); s.add_argument("--priority", type=int, default=100)
    s.set_defaults(fn=cmd_add_feature)
    s = hs.add_parser("seal", help="owner-approved re-seal after editing protected files (logged)")
    s.add_argument("--reason", required=True); s.add_argument("--approver", required=True)
    s.add_argument("--yes", action="store_true", help="confirm without a prompt (required when not on a terminal)")
    s.set_defaults(fn=cmd_seal)
    s = hs.add_parser("seed", help="seed gates and features from SpecLine plans / ForgeLine smoke manifests")
    s.add_argument("--from", dest="source", default="all", choices=["specline", "forgeline", "all"])
    s.add_argument("--feature", required=True); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_seed)
    for parser in hs.choices.values():
        parser.add_argument("--root", default=".")
