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
