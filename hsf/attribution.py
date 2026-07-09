"""Build-time-only failure attribution. This module is never compiled into artifacts."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from enum import Enum


class FailureClass(str, Enum):
    AMBIGUOUS_REQUIREMENT = "ambiguous_requirement"
    UNTYPED_INPUT = "untyped_input"
    SCOPE_ESCAPE = "scope_escape"
    INVENTED_PARAM = "invented_param"
    SIGNATURE_DRIFT = "signature_drift"
    STUB_UNFILLED = "stub_unfilled"
    COMPLEXITY_EXCEEDED = "complexity_exceeded"
    INCONSISTENT_LOGIC = "inconsistent_logic"
    RUNTIME_CRASH = "runtime_crash"
    RUNTIME_TIMEOUT = "runtime_timeout"
    WRONG_OUTPUT = "wrong_output"
    ACCURACY_REGRESSION = "accuracy_regression"
    NONDETERMINISM = "nondeterminism"
    SECURITY_FINDING = "security_finding"


@dataclass(frozen=True)
class UnitResult:
    unit: str
    stage: str
    passed: bool
    evidence: str
    failure_class: FailureClass | None = None

    def __post_init__(self):
        if not self.passed and (self.failure_class is None or not self.evidence.strip()):
            raise ValueError("failed units require a class and concrete evidence")


@dataclass
class Attribution:
    stage: str
    n_checked: int
    n_passed: int
    units: list[UnitResult]

    @property
    def rate(self):
        return self.n_passed / self.n_checked if self.n_checked else 0.0

    def dominant_failure_class(self):
        counts = {kind: 0 for kind in FailureClass}
        for unit in self.units:
            if not unit.passed:
                counts[unit.failure_class] += 1
        maximum = max(counts.values(), default=0)
        return next((kind for kind in FailureClass if maximum and counts[kind] == maximum), None)

    def to_dict(self):
        value = asdict(self)
        value["rate"] = self.rate
        dominant = self.dominant_failure_class()
        value["dominant_failure_class"] = dominant.value if dominant else None
        return value
