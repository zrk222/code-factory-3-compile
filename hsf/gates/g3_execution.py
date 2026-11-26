"""Gate 3 — Execution: sandboxed subprocess run + 3x determinism check.

Sandbox: separate process, empty env, CPU/AS rlimits, socket disabled
before artifact import, cwd in a throwaway tmpdir, fixture extractor only.
"""
from __future__ import annotations
import json, subprocess, sys, tempfile, textwrap, time
from pathlib import Path
from .base import GateResult, Finding
from .sandbox_env import minimal_env

RUNNER = textwrap.dedent("""
    import asyncio, builtins, json, os, socket, sys
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.selector_events.BaseSelectorEventLoop._close_self_pipe = lambda self: None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # resource limits are Unix-only; degrade gracefully on Windows (module absent).
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024, 512*1024*1024))
    except (ImportError, ValueError, OSError):
        pass  # Windows / restricted env: rely on the parent-process timeout instead
    def _blocked(*a, **k): raise RuntimeError("network blocked in sandbox")
    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.socket.sendto = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked
    _open = builtins.open
    def _ro_open(f, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            raise RuntimeError("file writes blocked in sandbox")
        return _open(f, mode, *a, **k)
    builtins.open = _ro_open

    artifact_path, fixtures_json = sys.argv[1], sys.argv[2]
    src = _open(artifact_path, encoding="utf-8").read()
    ns = {}
    exec(compile(src, artifact_path, "exec"), ns)
    cls = next(v for k, v in ns.items() if k.endswith("Workflow") and isinstance(v, type))
    if len(sys.argv) > 3 and [getattr(cls, "SPEC_ID", None), getattr(cls, "SPEC_SHA", None)] != sys.argv[3:]:
        raise ValueError("runtime workflow identity differs from the loaded spec")

    class Ctx:
        def __init__(self, fields): self.fields = fields
        async def sandwich(self, schema, text): return dict(self.fields)
        async def out_of_bounds(self, msg, trace="", policy="human_review"):
            status = "DENIED" if policy == "reject" else "HUMAN_REVIEW"
            return ns["AuthResult"](status, msg)

    async def main():
        outs = []
        for case in json.loads(_open(fixtures_json, encoding="utf-8").read()):
            r = await cls().run(Ctx(case["extracted"]), {"text": case["input_text"]})
            outs.append({"status": r.status, "reason": r.reason})
        print(json.dumps(outs, sort_keys=True))
    loop.run_until_complete(main())
""")

def run(source: str, smoke_cases: list[dict], repeats: int = 3, *,
        capture_outputs=False, spec_identity: tuple[str, str] | None = None) -> GateResult:
    findings, outputs = [], []
    with tempfile.TemporaryDirectory() as td:
        art = Path(td) / "artifact.py"
        art.write_text(source, encoding="utf-8")
        fx = Path(td) / "fixtures.json"
        fx.write_text(json.dumps(smoke_cases), encoding="utf-8")
        runner = Path(td) / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        argv = [sys.executable, str(runner), str(art), str(fx), *(spec_identity or ())]
        # Separate processes are what the determinism claim rests on; they start together and are judged in
        # order, each against its own 30-second deadline.
        started = time.monotonic()
        procs = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                                  env=minimal_env(), cwd=td) for _ in range(repeats)]
        try:
            for i, proc in enumerate(procs):
                try:
                    stdout, stderr = proc.communicate(timeout=max(0.1, started + 30 - time.monotonic()))
                except subprocess.TimeoutExpired:
                    findings.append(Finding("HSF-EXE-002", "critical", f"run {i+1} exceeded 30 seconds"))
                    break
                if proc.returncode != 0:
                    findings.append(Finding("HSF-EXE-001", "critical", f"run {i+1} failed: {stderr[-400:]}"))
                    break
                outputs.append(stdout.strip())
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
    if not findings and len(set(outputs)) > 1:
        findings.append(Finding("HSF-DET-001", "critical", "nondeterministic output across identical runs"))
    evidence: dict[str, object] = {"repeats": repeats, "byte_identical": bool(outputs) and len(set(outputs)) == 1}
    if capture_outputs and not findings:
        try:
            decoded = json.loads(outputs[0])
            if not isinstance(decoded, list) or len(decoded) != len(smoke_cases) or any(
                not isinstance(row, dict) or set(row) != {"status", "reason"}
                or any(not isinstance(value, str) for value in row.values()) for row in decoded
            ):
                raise ValueError("unexpected decision output shape or count")
            evidence["outputs"] = decoded
        except (IndexError, ValueError) as error:
            findings.append(Finding("HSF-EXE-003", "critical", f"invalid sandbox output: {error}"))
    return GateResult("execution", not findings, findings, evidence)
