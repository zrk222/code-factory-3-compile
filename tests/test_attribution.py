from copy import deepcopy
import hashlib
from hsf.gates.g4_accuracy import run


def test_g4_strictness_categories_and_first_divergence(artifact_source, goldens):
    cases = deepcopy(goldens[:3])
    cases[0]["case_id"] = "c010"
    cases[0]["category"] = "contraindication"
    cases[1]["case_id"] = "c003"
    cases[1]["category"] = "contraindication"
    cases[2]["case_id"] = "c001"
    cases[0]["expected"]["status"] = "__WRONG__"
    cases[1]["expected"]["status"] = "__WRONG__"
    result = run(artifact_source, cases)
    assert result.passed is False
    assert result.evidence["accuracy"] == 1 / 3
    assert result.evidence["by_category"]["contraindication"]["rate"] == 0.0
    assert result.evidence["by_category"]["uncategorized"]["rate"] == 1.0
    assert result.evidence["first_divergence"]["case_id"] == "c003"
    assert result.evidence["attribution"]["dominant_failure_class"] == "wrong_output"


def test_category_metadata_does_not_mutate_artifact(artifact_source, goldens):
    before = hashlib.sha256(artifact_source.encode()).hexdigest()
    cases = deepcopy(goldens[:2])
    for item in cases:
        item["category"] = "metadata-only"
    run(artifact_source, cases)
    assert hashlib.sha256(artifact_source.encode()).hexdigest() == before


def test_execution_failure_still_has_classified_attribution(goldens):
    result = run("raise RuntimeError('broken artifact')", goldens[:1])
    assert result.passed is False
    assert result.evidence["attribution"]["n_checked"] == 1
    assert result.evidence["attribution"]["dominant_failure_class"] == "runtime_crash"


def test_compile_sha_identical_before_and_after_attribution(spec_and_sha, goldens):
    from hsf.foundry.compiler import compile_spec
    spec, spec_sha = spec_and_sha
    before_source, _ = compile_spec(spec, spec_sha, engine="template")
    before_sha = hashlib.sha256(before_source.encode()).hexdigest()
    categorized = deepcopy(goldens[:3])
    for fixture in categorized:
        fixture["category"] = "metadata-only"
    assert run(before_source, categorized).passed
    after_source, _ = compile_spec(spec, spec_sha, engine="template")
    after_sha = hashlib.sha256(after_source.encode()).hexdigest()
    assert after_sha == before_sha
    for symbol in ("attribution", "refine", "rejection_ledger", "edit_selection"):
        assert symbol not in after_source
