"""Content-addressed artifact store with detached signatures.

v1 signing: HMAC-SHA256 with a locally generated key (zero heavy deps,
proves the verify-before-load discipline). ed25519 via `cryptography`
is the documented upgrade path — the interface is identical.
"""
from __future__ import annotations
import hashlib, hmac, secrets
from pathlib import Path

class RegistryError(RuntimeError):
    pass

def _key(registry_dir: Path) -> bytes:
    kp = registry_dir / ".signing.key"
    if not kp.exists():
        registry_dir.mkdir(parents=True, exist_ok=True)
        kp.write_bytes(secrets.token_bytes(32))
        kp.chmod(0o600)
    return kp.read_bytes()

def artifact_sha(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()

def store_artifact(source: str, registry_dir: Path, spec_id: str) -> tuple[Path, str]:
    registry_dir = Path(registry_dir)
    sha = artifact_sha(source)
    path = registry_dir / f"{spec_id}-{sha[:12]}.py"
    registry_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    sig = hmac.new(_key(registry_dir), source.encode(), hashlib.sha256).hexdigest()
    path.with_suffix(".py.sig").write_text(sig)
    return path, sha

def verify_artifact(path: Path) -> None:
    path = Path(path)
    sig_path = path.with_suffix(".py.sig")
    if not sig_path.exists():
        raise RegistryError(f"E_UNSIGNED_ARTIFACT: {path.name} has no signature")
    expected = hmac.new(_key(path.parent), path.read_text().encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_path.read_text().strip()):
        raise RegistryError(f"E_BAD_SIGNATURE: {path.name} signature mismatch")
