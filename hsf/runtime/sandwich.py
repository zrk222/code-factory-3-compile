"""Safety Sandwich (PRD 6.7): deterministic validation on both sides of the
one permitted probabilistic step.

(a) input validation: size cap, encoding, injection heuristics (flag-only)
(b) quarantined extractor call (schema-locked JSON)
(c) strict parse: exactly the schema fields, correct primitive types
(d) bounds enforcement compiled from spec min/max
(e) on_out_of_bounds policy execution (by the artifact's compiled guards)
"""
from __future__ import annotations
import re, math
from .injection import scan_codes as _scan_injection

MAX_INPUT_CHARS = 50_000

class SandwichViolation(ValueError):
    pass

def validate_input(text: str) -> list[str]:
    if not isinstance(text, str):
        raise SandwichViolation("input must be str")
    if len(text) > MAX_INPUT_CHARS:
        raise SandwichViolation(f"input exceeds {MAX_INPUT_CHARS} chars")
    text.encode("utf-8")
    # flags, not blocks — the decision logic is static code; nothing to hijack.
    # Shared, deepened, categorised detection surface (single source of truth).
    return _scan_injection(text)

_PY = {"boolean": bool, "float": (int, float), "int": int, "string": str}

def strict_parse(schema: dict, payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if set(payload) != set(schema):
        return None  # extra="forbid" + missing-field rejection
    out = {}
    for k, fs in schema.items():
        t = fs["type"] if isinstance(fs, dict) else fs
        v = payload[k]
        if v is None or not isinstance(v, _PY[t]) or (t != "boolean" and isinstance(v, bool)):
            return None
        try:
            out[k] = _finite_float(v) if t == "float" else v
        except (ValueError, OverflowError):
            return None
    return out

def _finite_float(value):
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("nonfinite float")
    return converted


class SafetySandwich:
    def __init__(self, extractor):
        self.extractor = extractor
        self.flags: list[str] = []

    async def invoke(self, schema: dict, text: str) -> dict | None:
        self.flags = validate_input(text)                       # (a)
        raw = await self.extractor.extract(schema, text)        # (b)
        return strict_parse(schema, raw)                        # (c); (d)+(e) compiled in artifact
