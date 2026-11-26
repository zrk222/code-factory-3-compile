"""harness.yaml: the declared shape of an app harness (gates, protected and mutable surfaces, repair policy)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

GateKind = Literal["lint", "type", "unit", "e2e", "golden", "build", "custom"]
MAX_REPAIR_ATTEMPTS = 5


class GateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    run: str = Field(min_length=1)
    kind: GateKind = "custom"
    timeout_s: int = Field(default=600, ge=1, le=7200)
    required: bool = True
    flake_retries: int = Field(default=1, ge=0, le=3)
    inputs: list[str] = Field(default_factory=list)


class RepairPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=MAX_REPAIR_ATTEMPTS)
    timeout_s: int = Field(default=1800, ge=1, le=14400)
    reinit_on_environment_failure: bool = True


class HarnessSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    harness: str = Field(pattern=r"^[A-Za-z][\w.-]{0,63}$")
    version: Literal[1] = 1
    target: Literal["worker", "web", "mobile", "agent-ui", "generic"] = "generic"
    init: list[str] = Field(default_factory=list)
    gates: list[GateSpec] = Field(min_length=1)
    protected: list[str] = Field(default_factory=lambda: [
        "tests/**", "test/**", "e2e/**", "goldens/**", "specs/**", "**/*.test.*", "**/*.spec.*", "**/__tests__/**",
        "**/conftest.py", "pytest.ini", "harness/harness.yaml", "harness/features.json", "harness/*.lock.json"])
    unobserved: list[str] = Field(default_factory=lambda: ["node_modules", "__pycache__", ".pytest_cache",
                                                          ".mypy_cache", ".next", ".expo"])
    mutable: list[str] = Field(min_length=1)
    repair: RepairPolicy = Field(default_factory=RepairPolicy)
    features_path: str = "harness/features.json"
    progress_path: str = "harness/progress.jsonl"
    receipts_dir: str = "harness/receipts"
    parallel_gates: int = Field(default=1, ge=1, le=16)

    @field_validator("gates")
    @classmethod
    def _unique_gates(cls, gates: list[GateSpec]) -> list[GateSpec]:
        ids = [gate.id for gate in gates]
        if len(ids) != len(set(ids)):
            raise ValueError("gate ids must be unique")
        return gates

    def gate(self, gate_id: str) -> GateSpec:
        for gate in self.gates:
            if gate.id == gate_id:
                return gate
        raise KeyError(f"unknown gate {gate_id!r}")


def load_harness(root: Path) -> HarnessSpec:
    path = Path(root) / "harness" / "harness.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found; run `hsf harness init <name>` first")
    return HarnessSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
