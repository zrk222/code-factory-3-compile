"""Feature ledger: JSON features whose only agent-writable field is `passes`, sealed by a lock digest."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

LOCK_NAME = "ledger.lock.json"


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][\w.-]{0,63}$")
    category: str = "functional"
    description: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    gates: list[str] = Field(min_length=1)
    priority: int = 100
    passes: bool = False


def _canonical(features: list[Feature]) -> str:
    sealed = [feature.model_dump(exclude={"passes"}) for feature in features]
    return sha256(json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class Ledger:
    """Load, verify and update the ledger without letting content other than `passes` drift."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.parent / LOCK_NAME

    def load(self) -> list[Feature]:
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        features = [Feature.model_validate(row) for row in rows]
        if len({feature.id for feature in features}) != len(features):
            raise ValueError("duplicate feature id in ledger")
        return features

    def seal(self, features: list[Feature]) -> None:
        self.path.write_text(json.dumps([f.model_dump() for f in features], indent=2) + "\n", encoding="utf-8")
        self.lock_path.write_text(json.dumps({"sealed_sha256": _canonical(features)}, indent=2) + "\n",
                                  encoding="utf-8")

    def verify(self) -> list[Feature]:
        features = self.load()
        expected = json.loads(self.lock_path.read_text(encoding="utf-8"))["sealed_sha256"]
        if _canonical(features) != expected:
            raise ValueError("ledger content changed outside `passes`; re-seal with `hsf harness add-feature`")
        return features

    def next_feature(self) -> Feature | None:
        pending = [feature for feature in self.verify() if not feature.passes]
        return min(pending, key=lambda feature: (feature.priority, feature.id)) if pending else None

    def set_passes(self, feature_id: str, value: bool) -> Feature:
        features = self.verify()
        match = next((feature for feature in features if feature.id == feature_id), None)
        if match is None:
            raise KeyError(f"unknown feature {feature_id!r}")
        match.passes = value
        self.path.write_text(json.dumps([f.model_dump() for f in features], indent=2) + "\n", encoding="utf-8")
        return match

    def add(self, feature: Feature) -> None:
        features = self.verify()
        if any(existing.id == feature.id for existing in features):
            raise ValueError(f"feature {feature.id!r} already exists")
        self.seal(features + [feature])
