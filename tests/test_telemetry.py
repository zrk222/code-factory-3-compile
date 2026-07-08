import pytest
from hsf.telemetry import break_even, context_token_report, entropy_check, module_token_report, token_savings
from hsf.telemetry.metrics import DeterminismViolation

def test_break_even_report_matches_formula():
    r = break_even(compile_tokens=34000, interpretive_tokens_per_tx=2000, runtime_tokens_per_tx=0)
    assert r["n_star"] == 17.0
    assert r["tco_compiled_1m"] < r["tco_interpretive_1m"] / 40  # >40x reduction at 1M tx

def test_entropy_check_raises_on_divergence():
    entropy_check([{"a": 1}, {"a": 1}])
    with pytest.raises(DeterminismViolation):
        entropy_check([{"a": 1}, {"a": 2}])

def test_module_token_report_is_per_module_and_honest_about_exactness():
    report = module_token_report([
        ("concepts/a.md", "hello world"),
        ("templates/b.j2", "{{ value }}"),
    ])
    assert report["total_tokens"] > 0
    assert set(report["modules"]) == {"concepts", "templates"}
    assert "exact" in report
    assert report["method"] in {"tiktoken", "chars_per_token_estimate"}

def test_context_token_report_records_budget_and_pruning():
    report = context_token_report(max_tokens=32000)
    assert report["budget_tokens"] == 32000
    assert report["total_tokens"] > 0
    assert "modules" in report

def test_token_savings_uses_metered_compile_tokens():
    report = token_savings(compile_input_tokens=100, compile_output_tokens=20, interpretive_tokens_per_tx=20)
    assert report["compile_total_tokens"] == 120
    assert report["n_star"] == 6.0
