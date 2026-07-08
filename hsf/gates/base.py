from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    file: str = "<artifact>"
    line: int = 0

@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    findings: list[Finding] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"gate": self.gate, "passed": self.passed,
                "findings": [f.__dict__ for f in self.findings]}
