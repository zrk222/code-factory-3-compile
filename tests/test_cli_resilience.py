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
