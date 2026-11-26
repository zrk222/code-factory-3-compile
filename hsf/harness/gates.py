"""Run declared verification gates, detect flakes, classify failures and fingerprint them."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .spec import GateSpec

TAIL_CHARS = 4000
_ENVIRONMENT = re.compile(r"command not found|not recognized as an internal|No module named|ModuleNotFoundError|"
                          r"Cannot find module|ECONNREFUSED|Connection refused|EADDRINUSE|ENOENT|"
                          r"could not connect|No such file or directory", re.I)
_BUILD = re.compile(r"SyntaxError|IndentationError|TypeError: .* is not a function|error TS\d+|"
                    r"compilation failed|Build failed|cannot find symbol|ImportError", re.I)
_VOLATILE = re.compile(r"\b0x[0-9a-f]+\b|\b\d+(\.\d+)?\s*(ms|s|sec|seconds)\b|\b\d{4}-\d{2}-\d{2}[T ][\d:.]+\S*|"
                       r"/tmp/[^\s:]+|line \d+|:\d+:\d+", re.I)


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


def gate_env() -> dict:
    """Gates judge the bytes on disk: no bytecode cache can stand in for a repaired source file."""
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def _execute(gate: GateSpec, root: Path) -> tuple[int | None, str, float]:
    started = time.monotonic()
    try:
        done = subprocess.run(gate.run, shell=True, cwd=root, capture_output=True, text=True, env=gate_env(),
                              errors="replace", timeout=gate.timeout_s)
        return done.returncode, (done.stdout + done.stderr)[-TAIL_CHARS:], time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        parts = [part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
                 for part in (exc.stdout, exc.stderr)]
        return None, "".join(parts)[-TAIL_CHARS:], time.monotonic() - started


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


def run_gate(gate: GateSpec, root: Path) -> GateRun:
    code, out, took = _execute(gate, root)
    attempts = 1
    while code != 0 and attempts <= gate.flake_retries:
        retry_code, retry_out, retry_took = _execute(gate, root)
        attempts += 1
        took += retry_took
        if retry_code == 0:
            return GateRun(gate.id, gate.kind, True, 0, took, retry_out, "flake", "", attempts)
        code, out = retry_code, retry_out
    if code == 0:
        return GateRun(gate.id, gate.kind, True, 0, took, out, "ok", "", attempts)
    kind = classify(code, out)
    return GateRun(gate.id, gate.kind, False, code, took, out, kind, signature(gate.id, kind, out), attempts)


CACHE_PATH = ".hsf-harness/gate-cache.json"


def cache_key(gate: GateSpec, root: Path) -> str | None:
    """Digest of the gate command plus every declared input file; None when the gate declares no inputs."""
    if not gate.inputs:
        return None
    from .guard import matches, tree_digests
    files = {path: digest for path, digest in tree_digests(root).items() if matches(path, gate.inputs)}
    blob = json.dumps({"run": gate.run, "files": files}, sort_keys=True)
    return sha256(blob.encode("utf-8")).hexdigest()


def _load_cache(root: Path) -> dict:
    path = Path(root) / CACHE_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(root: Path, cache: dict) -> None:
    path = Path(root) / CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def _cached_or_run(gate: GateSpec, root: Path, cache: dict, use_cache: bool) -> tuple[GateRun, str | None]:
    key = cache_key(gate, root) if use_cache else None
    if key is not None and cache.get(gate.id) == key:
        return GateRun(gate.id, gate.kind, True, 0, 0.0, "", "cached", "", 0), key
    return run_gate(gate, root), key


def run_gates(gates, root: Path, only: list[str] | None = None, *, parallel: int = 1,
              use_cache: bool = True) -> list[GateRun]:
    """Run the selected gates (in parallel when asked), reusing a pass whose inputs are byte-identical."""
    selected = [gate for gate in gates if only is None or gate.id in only]
    cache = _load_cache(root) if use_cache else {}
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(selected) or 1))) as pool:
        results = list(pool.map(lambda gate: _cached_or_run(gate, root, cache, use_cache), selected))
    for gate, (run, key) in zip(selected, results):
        if key is not None and run.passed and run.classification != "flake":
            cache[gate.id] = key
        elif not run.passed:
            cache.pop(gate.id, None)
    if use_cache:
        _save_cache(root, cache)
    return [run for run, _ in results]


def failing(runs: list[GateRun], gates) -> list[GateRun]:
    required = {gate.id for gate in gates if gate.required}
    return [run for run in runs if not run.passed and run.gate in required]
