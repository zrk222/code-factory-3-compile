"""Gate 3 — Execution: sandboxed subprocess run + 3x determinism check.

Sandbox: separate process, empty env, CPU/AS rlimits, socket disabled
before artifact import, cwd in a throwaway tmpdir, fixture extractor only.
"""
from __future__ import annotations
import json, subprocess, sys, tempfile, textwrap
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
    src = _open(artifact_path).read()
    ns = {}
    exec(compile(src, artifact_path, "exec"), ns)
    cls = next(v for k, v in ns.items() if k.endswith("Workflow") and isinstance(v, type))

    class Ctx:
        def __init__(self, fields): self.fields = fields
        async def sandwich(self, schema, text): return dict(self.fields)
        async def out_of_bounds(self, msg, trace="", policy="human_review"):
            status = "DENIED" if policy == "reject" else "HUMAN_REVIEW"
            return ns["AuthResult"](status, msg)

    async def main():
        outs = []
        for case in json.loads(_open(fixtures_json).read()):
            r = await cls().run(Ctx(case["extracted"]), {"text": case["input_text"]})
            outs.append({"status": r.status, "reason": r.reason})
        print(json.dumps(outs, sort_keys=True))
    loop.run_until_complete(main())
""")

def run(source: str, smoke_cases: list[dict], repeats: int = 3) -> GateResult:
    findings, outputs = [], []
    with tempfile.TemporaryDirectory() as td:
        art = Path(td) / "artifact.py"
        art.write_text(source)
        fx = Path(td) / "fixtures.json"
        fx.write_text(json.dumps(smoke_cases))
        runner = Path(td) / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        for i in range(repeats):
            proc = subprocess.run([sys.executable, str(runner), str(art), str(fx)],
                                  capture_output=True, text=True, env=minimal_env(), cwd=td, timeout=30)
            if proc.returncode != 0:
                findings.append(Finding("HSF-EXE-001", "critical", f"run {i+1} failed: {proc.stderr[-400:]}"))
                break
            outputs.append(proc.stdout.strip())
    if not findings and len(set(outputs)) > 1:
        findings.append(Finding("HSF-DET-001", "critical", "nondeterministic output across identical runs"))
    return GateResult("execution", not findings, findings,
                      {"repeats": repeats, "byte_identical": len(set(outputs)) == 1 if outputs else False})
