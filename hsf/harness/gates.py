"""Run declared verification gates, detect flakes, classify failures and fingerprint them."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable

from .spec import GateSpec

TAIL_CHARS = 4000
CACHE_PATH = ".hsf-harness/gate-cache.json"
_ENVIRONMENT = re.compile(r"command not found|not recognized as an internal|No module named|ModuleNotFoundError|"
                          r"Cannot find module|ECONNREFUSED|Connection refused|EADDRINUSE|could not connect", re.I)
_BUILD = re.compile(r"SyntaxError|IndentationError|TypeError: .* is not a function|error TS\d+|"
                    r"compilation failed|Build failed|cannot find symbol|ImportError", re.I)
_VOLATILE = re.compile(r"\b0x[0-9a-f]+\b|\b\d+(\.\d+)?\s*(ms|s|sec|seconds)\b|\b\d{4}-\d{2}-\d{2}[T ][\d:.]+\S*|"
                       r"/tmp/[^\s:]+|line \d+|:\d+:\d+", re.I)
_SECRET = re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization|bearer)[\"']?\s*[:=]\s*"
                     r"[\"']?)([^\s\"',;]{6,})|\b(gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,})\b")
_BASE_ENV = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
             "COMSPEC", "PATHEXT", "VIRTUAL_ENV")


@dataclass
class GateRun:
    gate: str
    kind: str
    passed: bool
    exit_code: int | None
    duration_s: float
    output_tail: str
    classification: str = "ok"
    signature: str = ""
    attempts: int = 1

    def summary(self) -> dict:
        return {"gate": self.gate, "kind": self.kind, "passed": self.passed, "exit_code": self.exit_code,
                "duration_s": round(self.duration_s, 3), "classification": self.classification,
                "signature": self.signature, "attempts": self.attempts, "output_tail": self.output_tail}


def redact(text: str) -> str:
    """Mask obvious credentials before output reaches receipts or failure reports."""
    return _SECRET.sub(lambda m: (m.group(1) + "***") if m.group(1) else "***", text)


def gate_env(allow: list[str] | None = None) -> dict:
    """Gates judge the bytes on disk: no bytecode cache can stand in for a repaired source file. With an
    `env_allow` list, only a minimal base environment plus those names is passed through."""
    if allow is None:
        env = dict(os.environ)
    else:
        env = {name: os.environ[name] for name in (*_BASE_ENV, *allow) if name in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def run_command(command: str, root: Path, timeout_s: int, env: dict) -> tuple[int | None, str, float]:
    """Run a shell command in its own process group; a timeout kills the whole group (dev servers, browsers)."""
    started = time.monotonic()
    extra = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    proc = subprocess.Popen(command, shell=True, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", env=env, **extra)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        return proc.returncode, redact(out[-TAIL_CHARS:]), time.monotonic() - started
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, _ = proc.communicate()
        return None, redact((out or "")[-TAIL_CHARS:]), time.monotonic() - started


def classify(exit_code: int | None, output: str) -> str:
    if exit_code is None:
        return "timeout"
    if exit_code in (126, 127) or _ENVIRONMENT.search(output):
        return "environment"
    if _BUILD.search(output):
        return "build"
    return "verification"


def signature(gate_id: str, classification: str, output: str) -> str:
    lines = [_VOLATILE.sub("#", line).strip() for line in output.splitlines() if line.strip()]
    return sha256("\n".join([gate_id, classification, *lines[-20:]]).encode("utf-8")).hexdigest()[:16]


def run_gate(gate: GateSpec, root: Path, env: dict | None = None) -> GateRun:
    env = env or gate_env()
    code, out, took = run_command(gate.run, root, gate.timeout_s, env)
    attempts = 1
    while code != 0 and attempts <= (gate.flake_retries or 0):
        retry_code, retry_out, retry_took = run_command(gate.run, root, gate.timeout_s, env)
        attempts += 1
        took += retry_took
        if retry_code == 0:
            return GateRun(gate.id, gate.kind, True, 0, took, retry_out, "flake", "", attempts)
        code, out = retry_code, retry_out
    if code == 0:
        return GateRun(gate.id, gate.kind, True, 0, took, out, "ok", "", attempts)
    kind = classify(code, out)
    return GateRun(gate.id, gate.kind, False, code, took, out, kind, signature(gate.id, kind, out), attempts)


def cache_key(gate: GateSpec, root: Path, digests: dict[str, str] | None = None) -> str | None:
    """Digest of the gate command plus every declared input file; None when the gate declares no inputs."""
    if not gate.inputs:
        return None
    from .guard import matches, tree_digests
    digests = digests if digests is not None else tree_digests(root)
    files = {path: digest for path, digest in digests.items() if matches(path, gate.inputs)}
    return sha256(json.dumps({"run": gate.run, "files": files}, sort_keys=True).encode("utf-8")).hexdigest()


def _load_cache(root: Path) -> dict:
    try:
        return json.loads((Path(root) / CACHE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(root: Path, cache: dict) -> None:
    path = Path(root) / CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


Planner = Callable[[], "dict[str, str] | None"]
Recorder = Callable[[GateSpec, GateRun], object]


def _planned(gate: GateSpec, plan: dict[str, str] | None) -> GateRun | None:
    disposition = (plan or {}).get(gate.id)
    if disposition == "REUSE":
        return GateRun(gate.id, gate.kind, True, 0, 0.0, "", "reused", "", 0)
    if disposition == "SKIP":
        return GateRun(gate.id, gate.kind, True, 0, 0.0, "", "skipped", "", 0)
    return None


def run_gates(gates, root: Path, only: list[str] | None = None, *, parallel: int = 1, use_cache: bool = True,
              env: dict | None = None, planner: Planner | None = None, recorder: Recorder | None = None,
              digests: dict[str, str] | None = None) -> list[GateRun]:
    """Run the selected gates (in parallel when asked). A pass whose inputs are byte-identical is reused from
    the local cache, or from `factory proofs` when a planner is given."""
    selected = [gate for gate in gates if only is None or gate.id in only]
    plan = planner() if planner else None
    cache = _load_cache(root) if use_cache and planner is None else {}
    if use_cache and planner is None and digests is None and any(gate.inputs for gate in selected):
        from .guard import tree_digests
        digests = tree_digests(root)

    def one(gate: GateSpec) -> tuple[GateRun, str | None]:
        planned = _planned(gate, plan)
        if planned is not None:
            return planned, None
        key = cache_key(gate, root, digests) if use_cache and planner is None else None
        if key is not None and cache.get(gate.id) == key:
            return GateRun(gate.id, gate.kind, True, 0, 0.0, "", "cached", "", 0), key
        return run_gate(gate, root, env), key

    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(selected) or 1))) as pool:
        results = list(pool.map(one, selected))
    for gate, (run, key) in zip(selected, results):
        if key is not None and run.passed and run.classification == "ok":
            cache[gate.id] = key
        elif not run.passed:
            cache.pop(gate.id, None)
        if recorder and gate.read_only and run.passed and run.classification == "ok" and run.attempts:
            recorder(gate, run)
    if use_cache and planner is None:
        _save_cache(root, cache)
    return [run for run, _ in results]


def failing(runs: list[GateRun], gates) -> list[GateRun]:
    required = {gate.id for gate in gates if gate.required}
    return [run for run in runs if not run.passed and run.gate in required]
