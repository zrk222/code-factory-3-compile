"""Cross-platform portability regression tests (v0.2.1).

These lock in the two fixes that made HSF green on Windows:
  1. gate subprocesses use minimal_env() (not env={}), so python.exe launches
     on Windows AND the parent's secrets are never forwarded.
  2. the sandbox RUNNER guards the Unix-only `resource` module, so it runs on
     Windows instead of crashing on import.
"""
import os, sys, subprocess, tempfile, json
from pathlib import Path
from hsf.gates.sandbox_env import minimal_env
from hsf.gates.g3_execution import RUNNER


def test_minimal_env_is_launchable_and_secret_free(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-forward")
    monkeypatch.setenv("SECRET_TOKEN", "nope")
    env = minimal_env()
    # launchable: PATH present, interpreter dir on it
    assert "PATH" in env and os.path.dirname(sys.executable) in env["PATH"]
    # deterministic
    assert env["PYTHONHASHSEED"] == "0"
    # secret-free: application secrets never forwarded
    assert "OPENAI_API_KEY" not in env
    assert "SECRET_TOKEN" not in env


def test_minimal_env_has_windows_bootstrap_keys_when_present(monkeypatch):
    # emulate a Windows-ish parent env; SystemRoot must survive if present
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    env = minimal_env()
    # on nt these forward; on posix they're simply absent (both are fine)
    if os.name == "nt":
        assert "SystemRoot" in env


def test_runner_survives_missing_resource_module():
    """Emulate Windows: block `resource`, prove the RUNNER still executes."""
    with tempfile.TemporaryDirectory() as td:
        art = Path(td) / "a.py"
        art.write_text(
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class AuthResult:\n    status: str\n    reason: str\n"
            "class DemoWorkflow:\n"
            "    async def run(self, ctx, text):\n"
            "        return AuthResult('APPROVED', 'ok')\n")
        fx = Path(td) / "f.json"
        fx.write_text(json.dumps([{
            "case_id": "c1", "input_text": "x",
            "extracted": {}, "expected": {"status": "APPROVED", "reason": "ok"}}]))
        rn = Path(td) / "r.py"; rn.write_text(RUNNER, encoding="utf-8")
        block = Path(td) / "sitecustomize.py"
        block.write_text("import sys; sys.modules['resource'] = None\n")
        e = minimal_env({"PYTHONPATH": td})
        proc = subprocess.run([sys.executable, str(rn), str(art), str(fx)],
                              capture_output=True, text=True, env=e, cwd=td, timeout=30)
        assert proc.returncode == 0, f"runner failed with resource blocked: {proc.stderr[-300:]}"
        out = json.loads(proc.stdout)
        assert out[0]["status"] == "APPROVED"
