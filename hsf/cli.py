"""hsf CLI: validate | compile | run | goldens | bench  (stdlib argparse; zero extra deps)."""
from __future__ import annotations
import argparse, glob, json, sys
from pathlib import Path

class CliError(RuntimeError):
    pass

def _die(message: str, code: int = 2) -> None:
    print(f"hsf: {message}", file=sys.stderr)
    raise SystemExit(code)

_MISSING_HINTS = {
    "spec": "Create one with `hsf init <name>` or check the path.",
    "artifact": "Compile one with `hsf compile <spec>`, or list registry_store/.",
    "receipt": "Compile with `hsf compile <spec>` to write a receipt under receipts/.",
    "file": "Check the path, or run `hsf compile <spec>` to generate artifacts.",
}
_SPEC_NAME = __import__("re").compile(r"[a-z][a-z0-9_]*")


def _suggest_name(name: str) -> str:
    import re
    cleaned = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    return cleaned if cleaned and cleaned[0].isalpha() else f"wf_{cleaned}".rstrip("_")


def _compile_failure_hint(exc) -> str:
    """Name the first failing gate and finding from the remediation evidence (H-UX-5)."""
    for attempt in reversed(getattr(exc, "evidence", None) or []):
        for result in attempt.get("results", []):
            if not result.get("passed", True):
                finding = (result.get("findings") or [{}])[0]
                code = finding.get("code") if isinstance(finding, dict) else finding
                return f" (gate {result.get('gate')} failed: {code or 'see evidence'}; rerun with --json for full evidence)"
    return ""


def _resolve_existing(path_or_glob: str, *, kind: str = "file") -> Path:
    path = Path(path_or_glob)
    if path.exists():
        return path
    matches = sorted(Path(p) for p in glob.glob(path_or_glob))
    if matches:
        return matches[-1]
    raise CliError(f"{kind} not found: {path_or_glob}. {_MISSING_HINTS.get(kind, _MISSING_HINTS['file'])}")

def _load(spec_path):
    from hsf.spec import load_spec
    return load_spec(_resolve_existing(spec_path, kind="spec"))

_GOLDEN_KEYS = ("input_text", "extracted", "expected")


def _goldens_for(spec_id: str, root: Path | None = None) -> list[dict]:
    p = (root or Path(".")) / "goldens" / spec_id / "cases.jsonl"
    if not p.exists():
        raise CliError(f"goldens not found for spec_id={spec_id!r}: {p}")
    rows = []
    for number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise CliError(f"E_GOLDEN_SHAPE: {p}:{number}: not JSON ({exc.msg})") from exc
        missing = [key for key in _GOLDEN_KEYS if not isinstance(row, dict) or key not in row]
        expected = row.get("expected") if isinstance(row, dict) else None
        if missing or not isinstance(expected, dict) or not {"status", "reason"} <= set(expected):
            raise CliError(f"E_GOLDEN_SHAPE: {p}:{number}: each row needs input_text, extracted and "
                           f"expected{{status, reason}} (missing: {', '.join(missing) or 'expected.status/reason'})")
        rows.append(row)
    return rows

def _rooted(path: str, root: str | None) -> Path:
    """Resolve a relative output path against --root (H-CON-1); without --root keep today's cwd behaviour."""
    candidate = Path(path)
    return candidate if root is None or candidate.is_absolute() else Path(root) / candidate


def cmd_goldens_draft(args):
    """Draft golden inputs (never expected answers): one row per rule plus one out-of-bounds row (H-AUT-1)."""
    from hsf.goldens_draft import draft_cases
    spec_path = _resolve_existing(args.spec, kind="spec").resolve()
    spec, _ = _load(str(spec_path))
    root = Path(args.root).resolve() if args.root else spec_path.parent.parent
    out = root / "goldens" / spec.workflow_spec / "cases.draft.jsonl"
    if out.exists() and not args.force:
        raise CliError(f"{out} exists; review it or pass --force")
    rows = draft_cases(spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    print(f"drafted {len(rows)} case(s): {out}")
    print("fill every `expected`, then save the reviewed file as cases.jsonl; compile ignores the draft")


def _json_arg(raw: str, *, name: str) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"{name} must be valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CliError(f"{name} must decode to a JSON object")
    return value

def _schema_of(spec) -> dict:
    out = {}
    for s in spec.steps:
        if s.type == "bounded_invocation" and s.schema_:
            for k, fs in s.schema_.items():
                d = {"type": fs.type}
                if fs.min is not None: d["min"] = fs.min
                if fs.max is not None: d["max"] = fs.max
                if fs.min_inclusive: d["min_inclusive"] = True
                if fs.max_inclusive: d["max_inclusive"] = True
                out[k] = d
    return out

def cmd_validate(args):
    spec, sha = _load(args.spec)
    print(f"OK {spec.workflow_spec} v{spec.version} sha={sha[:16]}…")

def cmd_compile(args):
    from hsf.foundry.regeneration import compile_with_regeneration
    from hsf.gates.pipeline import run_pipeline, write_receipt
    from hsf.registry import store_artifact, RegistryError
    from hsf.spec import load_spec
    spec_path = _resolve_existing(args.spec, kind="spec").resolve()
    spec, sha = load_spec(spec_path, expected_file_sha256=args.expected_spec_file_sha256)
    root = Path(args.root).resolve() if args.root else spec_path.parent.parent
    goldens = _goldens_for(spec.workflow_spec, root)
    if not goldens:
        raise CliError("compile requires reviewed golden cases")
    schema = _schema_of(spec)
    smoke = goldens[:5]
    input_texts = [case["input_text"] for case in goldens[:10]]
    observation = None

    def gate_runner(source):
        nonlocal observation
        observation = run_pipeline(source, spec_schema=schema, smoke_cases=smoke,
                                   golden_cases=goldens, input_texts=input_texts,
                                   spec_identity=(spec.workflow_spec, sha))
        return observation

    src, meta, _ = compile_with_regeneration(
        spec, sha, gate_runner, engine=args.engine, policy=args.remediation
    )
    passed, results = observation
    if not passed or [result.gate for result in results] != ["security", "syntax", "execution", "accuracy"]:
        raise CliError("the candidate lacks a complete passing gate sequence")
    signing = {"signing_key": Path(args.signing_key)} if args.signing_key else {}
    try:
        path, art_sha = store_artifact(src, _rooted(args.registry, args.root), spec.workflow_spec, **signing)
    except RegistryError as exc:
        raise CliError(str(exc)) from exc
    if args.expected_spec_file_sha256 is not None:
        meta["spec_file_sha256"] = args.expected_spec_file_sha256
    receipt = write_receipt(_rooted("receipts", args.root), spec_id=spec.workflow_spec, spec_sha=sha,
                            artifact_sha=art_sha, compile_meta=meta, results=results, shipped=passed)
    if args.json:
        print(json.dumps({"shipped": passed, "artifact": str(path), "artifact_sha256": art_sha,
                          "receipt": str(receipt), "attempts": meta["attempts"], "engine": meta["engine"]}))
    else:
        print(f"COMPILED {path}\nRECEIPT  {receipt}\nshipped={passed} attempts={meta['attempts']}")


def cmd_run(args):
    from hsf.registry import verify_artifact, RegistryError
    from hsf.runtime import Orchestrator
    from hsf.runtime.extractor import FixtureExtractor
    fields = _json_arg(args.extracted, name="--extracted")
    artifact = _resolve_existing(args.artifact, kind="artifact")
    try:
        orch = Orchestrator(
            artifact, None if args.structured_only else FixtureExtractor(fields),
            verify=verify_artifact,
            trusted_public_key=Path(args.trusted_public_key) if args.trusted_public_key else None,
            expected_sha=args.expected_sha, structured_only=args.structured_only,
        )
        result = orch.run_structured(fields) if args.structured_only else orch.run({"text": args.text})
    except (RegistryError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    print(json.dumps({"status": result.status, "reason": result.reason}))

def cmd_goldens(args):
    from hsf.gates.g4_accuracy import run as g4
    artifact = _resolve_existing(args.artifact, kind="artifact")
    src = artifact.read_text(encoding="utf-8")
    r = g4(src, _goldens_for(args.spec_id, Path(args.root) if args.root else None))
    print(json.dumps(r.evidence, indent=2))
    sys.exit(0 if r.passed else 1)

def cmd_init(args):
    from hsf.scaffold import init_workflow
    if not _SPEC_NAME.fullmatch(args.name):
        raise CliError(f"workflow name {args.name!r} must be snake_case ([a-z][a-z0-9_]*); try {_suggest_name(args.name)!r}")
    try:
        paths = init_workflow(args.name)
    except FileExistsError as exc:
        raise CliError(f"{exc}; choose another name or edit the existing spec") from exc
    print("created:\n  " + "\n  ".join(str(x) for x in paths))
    print(f"next: hsf compile specs/{args.name}.yaml")

def cmd_demo(args):
    from hsf.demo import run_demo
    run_demo()

def cmd_serve(args):
    from hsf.serve import build_app
    try:
        import uvicorn
    except ImportError as exc:
        raise CliError("serve requires optional dependencies: python -m pip install 'code-factory-3-compile[serve]'") from exc
    artifact = _resolve_existing(args.artifact, kind="artifact")
    uvicorn.run(build_app(str(artifact)), host=args.host, port=args.port)

def cmd_badge(args):
    from hsf.badge import badge_from_receipt
    print(badge_from_receipt(_resolve_existing(args.receipt, kind="receipt")))

def cmd_aku(args):
    from hsf.aku import export_aku, validate_aku, write_aku
    spec, sha = _load(args.spec)
    receipt = _resolve_existing(args.receipt, kind="receipt") if args.receipt else None
    validation = validate_aku(spec, receipt_path=receipt, require_autonomous=args.require_autonomous)
    if not validation["passed"]:
        print(json.dumps(validation, indent=2))
        sys.exit(1)
    aku = export_aku(spec, sha, receipt_path=receipt)
    out = args.output or str(_rooted(f"{spec.workflow_spec}.aku.json", args.root))
    path = write_aku(aku, out)
    print(path)

def cmd_topology(args):
    from hsf.aku import validate_topology
    print(json.dumps(validate_topology(_resolve_existing(args.manifest, kind="topology manifest")), indent=2))

def cmd_bench(args):
    from hsf.telemetry import break_even
    print(json.dumps(break_even(args.compile_tokens), indent=2))

def cmd_meter(args):
    from hsf.telemetry import context_token_report
    print(json.dumps(context_token_report(max_tokens=args.max_tokens), indent=2))

def cmd_challenge(args):
    from hsf.challenge import challenge_spec
    spec_path = _resolve_existing(args.spec, kind="spec")
    root = Path(args.root).resolve() if args.root else spec_path.resolve().parent.parent
    spec, _ = _load(args.spec)
    golden_path = root / "goldens" / spec.workflow_spec / "cases.jsonl"
    payload = challenge_spec(spec_path, golden_path)
    out = Path(args.output) if args.output else root / ".hsf" / spec.workflow_spec / "challenge.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload | {"receipt_path": str(out)}, indent=2))
    if not payload["passed"]:
        raise SystemExit(1)

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--version":
        from hsf.provenance import provenance
        payload = provenance()
        print(json.dumps(payload, indent=2, sort_keys=True) if "--json" in argv else f"hsf {payload['version']}")
        return
    p = argparse.ArgumentParser(prog="hsf", description="Harness Software Factory")
    sub = p.add_subparsers(required=True, dest="cmd")
    s = sub.add_parser("validate"); s.add_argument("spec"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("compile"); s.add_argument("spec")
    s.add_argument("--engine", default="template", choices=["template", "llm"])
    s.add_argument("--expected-spec-file-sha256", help="pin the exact handoff YAML bytes")
    s.add_argument("--remediation", choices=["template-fallback", "off"], default="template-fallback")
    s.add_argument("--json", action="store_true", help="emit the artifact and gate receipt as JSON")
    s.add_argument("--registry", default="registry_store"); s.set_defaults(fn=cmd_compile)
    s.add_argument("--root", help="project root for goldens, registry and receipts (default: the spec's grandparent "
                   "for goldens and the current directory for outputs)")
    s.add_argument("--signing-key", help="Ed25519 private-key file; omitted uses legacy local HMAC")
    s = sub.add_parser("run"); s.add_argument("artifact"); s.add_argument("--text", default="")
    s.add_argument("--extracted", default=""); s.set_defaults(fn=cmd_run)
    s.add_argument("--trusted-public-key", help="operator-pinned Ed25519 public-key file")
    s.add_argument("--expected-sha", help="expected full artifact SHA-256")
    s.add_argument("--structured-only", action="store_true", help="validate structured fields without a model provider; requires pinned public key")
    s = sub.add_parser("goldens"); s.add_argument("artifact"); s.add_argument("spec_id"); s.set_defaults(fn=cmd_goldens)
    s.add_argument("--root", help="project root that holds goldens/")
    s = sub.add_parser("goldens-draft", help="draft golden inputs for review (expected answers are left null)")
    s.add_argument("spec"); s.add_argument("--root"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_goldens_draft)
    s = sub.add_parser("init"); s.add_argument("name"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("demo"); s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("serve"); s.add_argument("artifact")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8317)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("badge"); s.add_argument("receipt"); s.set_defaults(fn=cmd_badge)
    s = sub.add_parser("aku"); s.add_argument("spec")
    s.add_argument("--receipt", default=None)
    s.add_argument("-o", "--output", default=None)
    s.add_argument("--require-autonomous", action="store_true")
    s.add_argument("--root", help="directory for the default AKU output")
    s.set_defaults(fn=cmd_aku)
    s = sub.add_parser("topology"); s.add_argument("manifest"); s.set_defaults(fn=cmd_topology)
    s = sub.add_parser("bench"); s.add_argument("--compile-tokens", type=int, default=34000); s.set_defaults(fn=cmd_bench)
    s = sub.add_parser("meter"); s.add_argument("--max-tokens", type=int, default=32000); s.set_defaults(fn=cmd_meter)
    s = sub.add_parser("challenge"); s.add_argument("spec"); s.add_argument("-o", "--output", default=None); s.set_defaults(fn=cmd_challenge)
    s.add_argument("--root", help="project root that holds goldens/ and .hsf/")
    s = sub.add_parser("version"); s.add_argument("--json", action="store_true")
    from hsf.harness.cli import register as _register_harness
    _register_harness(sub)
    args = p.parse_args(argv)
    if args.cmd == "version":
        from hsf.provenance import provenance
        payload = provenance()
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else f"hsf {payload['version']}")
        return
    try:
        rc = args.fn(args)
        if args.cmd == "harness" and rc:
            raise SystemExit(rc)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        from hsf.spec import SpecError
        if args.cmd == "harness" or isinstance(exc, SpecError):
            if not (args.cmd == "compile" and getattr(args, "json", False)):
                _die(str(exc))
        if isinstance(exc, KeyError):
            raise
        if args.cmd != "compile" and not isinstance(exc, (CliError, SpecError)):
            raise
        if args.cmd == "compile" and args.json:
            result = {"shipped": False, "error": {"code": getattr(exc, "code", "E_COMPILE"),
                                                  "message": str(exc)}}
            if hasattr(exc, "evidence"):
                result["remediation_attempts"] = exc.evidence
            print(json.dumps(result))
            raise SystemExit(2) from None
        _die(str(exc) + (_compile_failure_hint(exc) if args.cmd == "compile" else ""))

if __name__ == "__main__":
    main()
