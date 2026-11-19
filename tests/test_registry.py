import pytest
from hsf.registry import store_artifact, verify_artifact, RegistryError

def test_unsigned_artifact_refused_at_load(tmp_path, artifact_source):
    path, _ = store_artifact(artifact_source, tmp_path, "glp1_review")
    path.with_suffix(".py.sig").unlink()
    with pytest.raises(RegistryError, match="E_UNSIGNED_ARTIFACT"):
        verify_artifact(path)

def test_tampered_artifact_refused(tmp_path, artifact_source):
    path, _ = store_artifact(artifact_source, tmp_path, "glp1_review")
    path.write_text(artifact_source + "\n# tampered\n")
    with pytest.raises(RegistryError, match="E_BAD_SIGNATURE"):
        verify_artifact(path)

def test_content_addressing_stable(tmp_path, artifact_source):
    p1, s1 = store_artifact(artifact_source, tmp_path, "glp1_review")
    p2, s2 = store_artifact(artifact_source, tmp_path, "glp1_review")
    assert s1 == s2 and p1 == p2

from pathlib import Path
import json
from hsf.registry import verified_artifact_bytes


@pytest.fixture
def keys():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw(), private.public_key().public_bytes_raw()


def test_public_signature_roundtrip(tmp_path, keys):
    source = "answer = 'café'\r\n"
    path, digest = store_artifact(source, tmp_path, "policy", signing_key=keys[0])
    assert verified_artifact_bytes(path, trusted_public_key=keys[1]) == source.encode()
    assert verify_artifact(path, trusted_public_key=keys[1]) is None
    assert not (tmp_path / ".signing.key").exists()
    original = path.with_suffix(".py.sig").read_bytes()
    assert store_artifact(source, tmp_path, "policy", signing_key=keys[0]) == (path, digest)
    assert path.with_suffix(".py.sig").read_bytes() == original


def test_public_missing_and_wrong_key(tmp_path, keys):
    path, _ = store_artifact("x=1", tmp_path, "policy", signing_key=keys[0])
    for key in (None, b"x" * 32, b"short"):
        with pytest.raises(RegistryError):
            verified_artifact_bytes(path, trusted_public_key=key)
    path.with_suffix(".py.sig").unlink()
    with pytest.raises(RegistryError, match="E_UNSIGNED_ARTIFACT"):
        verified_artifact_bytes(path, trusted_public_key=keys[1])


@pytest.mark.parametrize("field,value", [("algorithm", "none"), ("keyid", "bad"),
                                         ("digest", "bad"), ("signature", "AAAA")])
def test_envelope_tampering(tmp_path, keys, field, value):
    path, _ = store_artifact("x=1", tmp_path, "policy", signing_key=keys[0])
    sig = path.with_suffix(".py.sig")
    envelope = json.loads(sig.read_bytes())
    envelope[field] = value
    sig.write_text(json.dumps(envelope))
    with pytest.raises(RegistryError, match="E_BAD_SIGNATURE"):
        verified_artifact_bytes(path, trusted_public_key=keys[1])


def test_public_byte_tampering(tmp_path, keys):
    path, _ = store_artifact("x=1", tmp_path, "policy", signing_key=keys[0])
    path.write_bytes(b"x=2")
    with pytest.raises(RegistryError, match="digest"):
        verified_artifact_bytes(path, trusted_public_key=keys[1])


def test_hmac_downgrade_and_no_key_creation(tmp_path):
    path, _ = store_artifact("x=1\n", tmp_path, "policy")
    for options in ({"trusted_public_key": b"x" * 32}, {"allow_legacy_hmac": False}):
        with pytest.raises(RegistryError, match="legacy HMAC disabled"):
            verified_artifact_bytes(path, **options)
    (tmp_path / ".signing.key").unlink()
    before = sorted(tmp_path.iterdir())
    with pytest.raises(RegistryError, match="existing legacy key required"):
        verified_artifact_bytes(path)
    assert sorted(tmp_path.iterdir()) == before


def test_legacy_newlines_unicode(tmp_path):
    path, _ = store_artifact("x='café'\n", tmp_path, "policy")
    path.write_bytes("x='café'\r\n".encode())
    assert verified_artifact_bytes(path) == "x='café'\n".encode()


def test_source_read_once_and_replacement(tmp_path, keys, monkeypatch):
    path, _ = store_artifact("x=1", tmp_path, "policy", signing_key=keys[0])
    original = Path.read_bytes
    reads = []
    def replacing_read(target):
        data = original(target)
        if target == path:
            reads.append(target)
            target.write_bytes(b"x=999")
        return data
    monkeypatch.setattr(Path, "read_bytes", replacing_read)
    assert verified_artifact_bytes(path, trusted_public_key=keys[1]) == b"x=1"
    assert reads == [path]


@pytest.mark.parametrize("spec_id", ["../escape", "a/b", "a\\b", "", ".", ".."])
def test_spec_containment(tmp_path, spec_id):
    with pytest.raises(RegistryError, match="E_UNSAFE_PATH"):
        store_artifact("x=1", tmp_path, spec_id)


def test_reject_traversal(tmp_path):
    with pytest.raises(RegistryError, match="E_UNSAFE_PATH"):
        verified_artifact_bytes(tmp_path / "child" / ".." / "artifact.py")


def test_strict_envelope(tmp_path, keys):
    path, _ = store_artifact("x=1", tmp_path, "policy", signing_key=keys[0])
    sig = path.with_suffix(".py.sig")
    for invalid in ('{"algorithm":"Ed25519","algorithm":"Ed25519"}', '{"extra":1}', '{bad'):
        sig.write_text(invalid)
        with pytest.raises(RegistryError, match="E_BAD_SIGNATURE"):
            verified_artifact_bytes(path, trusted_public_key=keys[1])


def test_legacy_store_crlf(tmp_path):
    path, _ = store_artifact("x='café'\r\n", tmp_path, "policy")
    assert verified_artifact_bytes(path) == "x='café'\n".encode()


def test_symlink_refused(tmp_path, monkeypatch):
    path, _ = store_artifact("x=1", tmp_path, "policy")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda candidate: candidate == path or original(candidate))
    with pytest.raises(RegistryError, match="E_UNSAFE_PATH"):
        verified_artifact_bytes(path)
    with pytest.raises(RegistryError, match="E_UNSAFE_PATH"):
        store_artifact("x=1", tmp_path, "policy")
