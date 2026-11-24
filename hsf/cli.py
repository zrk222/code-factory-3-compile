"""hsf CLI: validate | compile | run | goldens | bench  (stdlib argparse; zero extra deps)."""
from __future__ import annotations
import argparse, glob, json, sys
from pathlib import Path

class CliError(RuntimeError):
    pass

def _die(message: str, code: int = 2) -> None:
    print(f"hsf: {message}", file=sys.stderr)
    raise SystemExit(code)

def _resolve_existing(path_or_glob: str, *, kind: str = "file") -> Path:
    path = Path(path_or_glob)
    if path.exists():
        return path
    matches = sorted(Path(p) for p in glob.glob(path_or_glob))
    if matches:
        return matches[-1]
    hint = " Use Get-ChildItem/ls to inspect generated files, or run `hsf compile <spec>` first."
    raise CliError(f"{kind} not found: {path_or_glob}.{hint}")

def _load(spec_path):
    from hsf.spec import load_spec
    return load_spec(_resolve_existing(spec_path, kind="spec"))

def _goldens_for(spec_id: str, root: Path | None = None) -> list[dict]:
    p = (root or Path(".")) / "goldens" / spec_id / "cases.jsonl"
    if not p.exists():
        raise CliError(f"goldens not found for spec_id={spec_id!r}: {p}")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

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
    root = spec_path.parent.parent
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
        path, art_sha = store_artifact(src, Path(args.registry), spec.workflow_spec, **signing)
    except RegistryError as exc:
        raise CliError(str(exc)) from exc
    if args.expected_spec_file_sha256 is not None:
        meta["spec_file_sha256"] = args.expected_spec_file_sha256
    receipt = write_receipt(Path("receipts"), spec_id=spec.workflow_spec, spec_sha=sha,
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
    r = g4(src, _goldens_for(args.spec_id))
    print(json.dumps(r.evidence, indent=2))
    sys.exit(0 if r.passed else 1)

def cmd_init(args):
    from hsf.scaffold import init_workflow
    paths = init_workflow(args.name)
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
    out = args.output or f"{spec.workflow_spec}.aku.json"
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
    root = spec_path.resolve().parent.parent
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
    s.add_argument("--signing-key", help="Ed25519 private-key file; omitted uses legacy local HMAC")
    s = sub.add_parser("run"); s.add_argument("artifact"); s.add_argument("--text", default="")
    s.add_argument("--extracted", default=""); s.set_defaults(fn=cmd_run)
    s.add_argument("--trusted-public-key", help="operator-pinned Ed25519 public-key file")
    s.add_argument("--expected-sha", help="expected full artifact SHA-256")
    s.add_argument("--structured-only", action="store_true", help="validate structured fields without a model provider; requires pinned public key")
    s = sub.add_parser("goldens"); s.add_argument("artifact"); s.add_argument("spec_id"); s.set_defaults(fn=cmd_goldens)
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
    s.set_defaults(fn=cmd_aku)
    s = sub.add_parser("topology"); s.add_argument("manifest"); s.set_defaults(fn=cmd_topology)
    s = sub.add_parser("bench"); s.add_argument("--compile-tokens", type=int, default=34000); s.set_defaults(fn=cmd_bench)
    s = sub.add_parser("meter"); s.add_argument("--max-tokens", type=int, default=32000); s.set_defaults(fn=cmd_meter)
    s = sub.add_parser("challenge"); s.add_argument("spec"); s.add_argument("-o", "--output", default=None); s.set_defaults(fn=cmd_challenge)
    s = sub.add_parser("version"); s.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "version":
        from hsf.provenance import provenance
        payload = provenance()
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else f"hsf {payload['version']}")
        return
    try:
        args.fn(args)
    except (ValueError, OSError, RuntimeError) as exc:
        if args.cmd != "compile" and not isinstance(exc, CliError):
            raise
        if args.cmd == "compile" and args.json:
            result = {"shipped": False, "error": {"code": getattr(exc, "code", "E_COMPILE"),
                                                  "message": str(exc)}}
            if hasattr(exc, "evidence"):
                result["remediation_attempts"] = exc.evidence
            print(json.dumps(result))
            raise SystemExit(2) from None
        _die(str(exc))

if __name__ == "__main__":
    main()
