"""Content-addressed artifacts with explicit public-key trust or local legacy HMAC."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path


class RegistryError(RuntimeError):
    pass


def artifact_sha(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _safe_path(path: Path) -> Path:
    path = Path(path).absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise RegistryError("E_UNSAFE_PATH: symlink or traversal")
    return path


def _material(value: Path | bytes) -> bytes:
    try:
        data = value if isinstance(value, bytes) else _safe_path(value).read_bytes()
    except OSError as exc:
        raise RegistryError("E_SIGNING_KEY: cannot read key") from exc
    if len(data) != 32:
        raise RegistryError("E_SIGNING_KEY: expected raw 32-byte key")
    return data


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:
        raise RegistryError("E_SIGNING_DEPENDENCY: Ed25519 requires cryptography") from exc
    return ed25519


def _key(registry_dir: Path, *, create: bool = False) -> bytes:
    kp = _safe_path(registry_dir / ".signing.key")
    if create and not kp.exists():
        kp.write_bytes(secrets.token_bytes(32))
        kp.chmod(0o600)
    try:
        return kp.read_bytes()
    except OSError as exc:
        raise RegistryError("E_SIGNING_KEY: existing legacy key required") from exc


def _legacy_bytes(data: bytes) -> bytes:
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _signed_envelope(data: bytes, key: Path | bytes) -> str:
    private = _ed25519().Ed25519PrivateKey.from_private_bytes(_material(key))
    public = private.public_key().public_bytes_raw()
    envelope = {"algorithm": "Ed25519", "keyid": hashlib.sha256(public).hexdigest(),
                "digest": hashlib.sha256(data).hexdigest(),
                "signature": base64.b64encode(private.sign(data)).decode("ascii")}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))


def store_artifact(source: str, registry_dir: Path, spec_id: str, *,
                   signing_key: Path | bytes | None = None) -> tuple[Path, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", spec_id):
        raise RegistryError("E_UNSAFE_PATH: invalid spec id")
    registry_dir = _safe_path(registry_dir)
    sha = artifact_sha(source)
    path = _safe_path(registry_dir / f"{spec_id}-{sha[:12]}.py")
    sig_path = _safe_path(path.with_suffix(".py.sig"))
    data = source.encode("utf-8")
    registry_dir.mkdir(parents=True, exist_ok=True)
    signature = (_signed_envelope(data, signing_key) if signing_key is not None else
                 hmac.new(_key(registry_dir, create=True), _legacy_bytes(data), hashlib.sha256).hexdigest())
    path.write_bytes(data)
    sig_path.write_bytes(signature.encode("ascii"))
    return path, sha


def _envelope(text: str) -> dict:
    try:
        def unique(pairs):
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError("duplicate keys")
            return result
        envelope = json.loads(text, object_pairs_hook=unique)
        if set(envelope) != {"algorithm", "keyid", "digest", "signature"}:
            raise ValueError("invalid fields")
        if not all(isinstance(value, str) for value in envelope.values()):
            raise ValueError("invalid values")
        return envelope
    except (ValueError, TypeError) as exc:
        raise RegistryError("E_BAD_SIGNATURE: invalid signature envelope") from exc


def _verify_public(data: bytes, signature: str, key: Path | bytes | None) -> None:
    envelope = _envelope(signature)
    if envelope["algorithm"] != "Ed25519":
        raise RegistryError("E_BAD_SIGNATURE: unsupported algorithm")
    if key is None:
        raise RegistryError("E_SIGNING_KEY: operator public key required")
    public = _material(key)
    if envelope["keyid"] != hashlib.sha256(public).hexdigest():
        raise RegistryError("E_BAD_SIGNATURE: key identity mismatch")
    if envelope["digest"] != hashlib.sha256(data).hexdigest():
        raise RegistryError("E_BAD_SIGNATURE: digest mismatch")
    try:
        raw_signature = base64.b64decode(envelope["signature"], validate=True)
        _ed25519().Ed25519PublicKey.from_public_bytes(public).verify(raw_signature, data)
    except RegistryError:
        raise
    except Exception as exc:
        raise RegistryError("E_BAD_SIGNATURE: Ed25519 signature mismatch") from exc


def verified_artifact_bytes(path: Path, *, trusted_public_key: Path | bytes | None = None,
                            allow_legacy_hmac: bool = True) -> bytes:
    path = _safe_path(path)
    sig_path = _safe_path(path.with_suffix(".py.sig"))
    try:
        signature = sig_path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"E_UNSIGNED_ARTIFACT: {path.name} has no signature") from exc
    if len(signature) > 2048:
        raise RegistryError("E_BAD_SIGNATURE: oversized signature")
    try:
        signature = signature.decode("ascii").strip()
        data = path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise RegistryError("E_BAD_SIGNATURE: unreadable artifact or signature") from exc
    if signature.startswith("{"):
        _verify_public(data, signature, trusted_public_key)
        return data
    if trusted_public_key is not None or not allow_legacy_hmac:
        raise RegistryError("E_BAD_SIGNATURE: legacy HMAC disabled")
    try:
        data = _legacy_bytes(data)
    except UnicodeError as exc:
        raise RegistryError("E_BAD_SIGNATURE: invalid legacy UTF-8") from exc
    expected = hmac.new(_key(path.parent), data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise RegistryError(f"E_BAD_SIGNATURE: {path.name} signature mismatch")
    return data


def verify_artifact(path: Path, *, trusted_public_key: Path | bytes | None = None,
                    allow_legacy_hmac: bool = True) -> None:
    verified_artifact_bytes(path, trusted_public_key=trusted_public_key,
                            allow_legacy_hmac=allow_legacy_hmac)
