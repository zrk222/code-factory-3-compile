import pytest


def test_python_m_hsf_entrypoint_imports():
    import hsf.__main__ as module

    assert callable(module.main)


def test_cli_reports_missing_artifact_without_traceback(capsys):
    from hsf.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["goldens", "registry_store/does-not-exist-*.py", "glp1_review"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "artifact not found" in err
    assert "hsf compile" in err


def test_cli_rejects_malformed_extracted_json(capsys, tmp_path):
    from hsf.cli import main

    artifact = tmp_path / "artifact.py"
    artifact.write_text("# placeholder", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["run", str(artifact), "--extracted", "{bad"])

    assert exc.value.code == 2
    assert "--extracted must be valid JSON" in capsys.readouterr().err


def test_cli_structured_profile_uses_pinned_signed_bytes(tmp_path, artifact_source, monkeypatch, capsys):
    import json
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from hsf.cli import main
    from hsf.registry import store_artifact

    monkeypatch.chdir(tmp_path)
    key = Ed25519PrivateKey.generate()
    public = tmp_path / "public.raw"
    public.write_bytes(key.public_key().public_bytes_raw())
    artifact, digest = store_artifact(artifact_source, tmp_path / "registry", "control",
                                     signing_key=key.private_bytes_raw())
    fields = {"has_t2d_diagnosis": True, "current_a1c": 7.2, "bmi": 28.0}
    main(["run", str(artifact), "--structured-only", "--trusted-public-key", str(public),
          "--expected-sha", digest, "--extracted", json.dumps(fields)])
    assert json.loads(capsys.readouterr().out) == {"status": "APPROVED", "reason": "T2D Diagnosis"}


def test_cli_structured_profile_rejects_legacy_downgrade(tmp_path, artifact_source, capsys):
    from hsf.cli import main
    from hsf.registry import store_artifact

    artifact, _ = store_artifact(artifact_source, tmp_path, "control")
    public = tmp_path / "public.raw"
    public.write_bytes(b"p" * 32)
    with pytest.raises(SystemExit) as exc:
        main(["run", str(artifact), "--structured-only", "--trusted-public-key", str(public)])
    assert exc.value.code == 2
    assert "legacy HMAC disabled" in capsys.readouterr().err
