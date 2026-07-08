import pytest
from hsf.telemetry import break_even, entropy_check
from hsf.telemetry.metrics import DeterminismViolation

def test_break_even_report_matches_formula():
    r = break_even(compile_tokens=34000, interpretive_tokens_per_tx=2000, runtime_tokens_per_tx=0)
    assert r["n_star"] == 17.0
    assert r["tco_compiled_1m"] < r["tco_interpretive_1m"] / 40  # >40x reduction at 1M tx

def test_entropy_check_raises_on_divergence():
    entropy_check([{"a": 1}, {"a": 1}])
    with pytest.raises(DeterminismViolation):
        entropy_check([{"a": 1}, {"a": 2}])
