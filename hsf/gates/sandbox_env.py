"""hsf.gates.sandbox_env — a portable, minimal environment for gate subprocesses.

The execution and accuracy gates run the compiled artifact in a subprocess with
a *minimal* environment, so a malicious or buggy artifact can't read the parent
process's secrets (API keys, tokens) out of os.environ.

The previous implementation passed ``env={}`` (completely empty). That is safe on
Linux but **breaks on Windows**: the OS cannot even launch ``python.exe`` without
``SystemRoot`` present, and often needs ``PATH``/``SYSTEMDRIVE`` too. The result
was ~15 Windows-only failures in the gate suite — the artifact never ran, so
execution + accuracy (and everything depending on them) failed.

This helper returns the *smallest* environment that is (a) valid on Windows, macOS
and Linux, and (b) still free of the parent's secrets. It allowlists only the OS
bootstrap variables — never the parent's application secrets.
"""
from __future__ import annotations
import os
import sys


# OS bootstrap variables required to launch an interpreter, per platform.
# NOTHING application-level (no *_API_KEY, no tokens) is ever forwarded.
_WINDOWS_KEYS = (
    "SystemRoot", "SYSTEMROOT", "windir", "SystemDrive", "SYSTEMDRIVE",
    "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)
_POSIX_KEYS = ("PATH", "TMPDIR", "LANG", "LC_ALL")


def minimal_env(extra: dict | None = None) -> dict:
    """Return a minimal, secret-free environment valid on the current platform.

    - On Windows: forwards only the OS bootstrap keys (SystemRoot, PATH, …).
    - On POSIX: forwards PATH (+ locale/tmp) so the interpreter resolves.
    - `extra`: explicit key/values the caller wants (e.g. PYTHONHASHSEED for
      determinism). Never pass secrets here.

    Application secrets in the parent env (API keys, tokens) are NEVER forwarded.
    """
    keys = _WINDOWS_KEYS if os.name == "nt" else _POSIX_KEYS
    env = {k: os.environ[k] for k in keys if k in os.environ}
    # Guarantee an interpreter directory is on PATH even if the parent lacked it.
    py_dir = os.path.dirname(sys.executable)
    if py_dir:
        sep = os.pathsep
        env["PATH"] = py_dir + (sep + env["PATH"] if env.get("PATH") else "")
    # Force deterministic hashing in the child so repeated runs are byte-identical
    # (this is what the execution gate's 3x determinism check depends on).
    env.setdefault("PYTHONHASHSEED", "0")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if extra:
        env.update(extra)
    return env
