"""hsf.runtime.injection — one shared, deepened injection-detection surface.

Before v0.2 the injection patterns lived in TWO places (runtime/sandwich.py and
gates/g1_security.py) with different, thin pattern sets. That is a drift hazard:
the gate and the runtime could disagree about what counts as an attack, and both
lists were far shallower than the factory's "there is no prompt to inject" claim
deserves.

This module is the single source of truth. Both the Safety Sandwich (runtime,
flag-only) and Gate 1 (factory, findings) import from here, so detection is
identical everywhere and deepens in one place.

Detection is CATEGORISED and CONFIDENCE-SCORED, not a flat regex list, so callers
can act proportionally (flag low-confidence, escalate high-confidence) and receipts
record *which class* of attack was seen. It never blocks in the runtime — the whole
HSF thesis is that the decision logic is static code, so an injection has nothing to
hijack; we flag for the audit trail, not to gate a transaction. The factory gate,
by contrast, treats a leak/exfil attempt in golden/spec inputs as a real finding.

Deterministic: pure regex + string ops, no model call, no clock. Same input ->
same findings, every run.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class InjectionHit:
    code: str          # stable class code, e.g. INJ_INSTRUCTION_OVERRIDE
    category: str      # human category
    confidence: str    # "high" | "medium" | "low"
    span: tuple[int, int]
    excerpt: str       # short, redaction-safe snippet of what matched

    def as_finding_dict(self) -> dict:
        return {"code": self.code, "category": self.category,
                "confidence": self.confidence, "at": self.span[0], "excerpt": self.excerpt}


# ---------------------------------------------------------------------------
# Pattern bank — categorised, each with a confidence weight.
# High = almost certainly an attack; medium = suspicious in extraction input;
# low = worth flagging but common in benign text.
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, str, str, str]] = [
    # (code, category, confidence, regex)
    ("INJ_INSTRUCTION_OVERRIDE", "instruction override", "high",
     r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|context)"),
    ("INJ_DISREGARD_SYSTEM", "instruction override", "high",
     r"disregard\s+.{0,30}(system|developer|prior)\s*(prompt|message|instructions)?"),
    ("INJ_FORGET_CONTEXT", "instruction override", "high",
     r"forget\s+(everything|all|what)\s+.{0,20}(said|told|above|before)"),
    ("INJ_ROLE_REASSIGN", "role reassignment", "high",
     r"you\s+are\s+now\s+(a|an|the)\b|from\s+now\s+on\s+you\s+(are|will|must)"),
    ("INJ_ACT_AS", "role reassignment", "medium",
     r"\b(act|behave|respond|pretend)\s+as\s+(if\s+)?(a|an|the|you)\b"),
    ("INJ_SYSTEM_IMPERSONATION", "delimiter / role spoof", "high",
     r"^\s*(system|developer|assistant)\s*:\s", ),
    ("INJ_FAKE_DELIMITER", "delimiter / role spoof", "medium",
     r"(<\s*/?\s*(system|instructions|prompt)\s*>)|(\[\s*(system|inst)\s*\])|(```\s*system)"),
    ("INJ_REVEAL_SECRETS", "exfiltration", "high",
     r"(reveal|show|print|repeat|output|leak)\s+.{0,30}(prompt|instructions|system\s*message|canary|api[\s_-]?key|secret|token)"),
    ("INJ_EXFIL", "exfiltration", "high",
     r"\bexfiltrat|send\s+.{0,20}(to|via)\s+.{0,20}(http|url|webhook|email)"),
    ("INJ_TOOL_HIJACK", "tool / action hijack", "high",
     r"(call|invoke|execute|run)\s+.{0,20}(tool|function|command|shell|os\.|subprocess)"),
    ("INJ_OVERRIDE_POLICY", "policy override", "medium",
     r"(override|bypass|turn\s+off|disable)\s+.{0,20}(safety|guardrail|filter|policy|restriction)"),
    ("INJ_JAILBREAK_FRAME", "jailbreak framing", "medium",
     r"\b(DAN|do\s+anything\s+now|developer\s+mode|jailbreak|unfiltered)\b"),
    ("INJ_ENCODED_PAYLOAD", "obfuscation", "low",
     r"(base64|rot13|\\x[0-9a-f]{2}|%[0-9a-f]{2}){1}.{0,40}(decode|eval|exec)"),
]

_COMPILED = [(code, cat, conf, re.compile(rx, re.IGNORECASE | re.MULTILINE))
             for (code, cat, conf, rx) in _PATTERNS]

# Zero-width / control characters used to smuggle instructions past eyeballing.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


def _normalize(text: str) -> str:
    """Fold unicode look-alikes and strip invisibles so obfuscated attacks still
    match. NFKC collapses full-width / styled characters to ASCII equivalents."""
    text = unicodedata.normalize("NFKC", text)
    return text


def scan(text: str) -> list[InjectionHit]:
    """Return all injection hits, categorised and confidence-scored. Deterministic."""
    if not isinstance(text, str) or not text:
        return []
    hits: list[InjectionHit] = []
    # 1. invisible-character smuggling is itself a signal
    for m in _INVISIBLE.finditer(text):
        hits.append(InjectionHit("INJ_INVISIBLE_CHAR", "obfuscation", "medium",
                                 (m.start(), m.end()), "<zero-width/control char>"))
    norm = _normalize(text)
    # 2. pattern bank over the normalized text
    for code, cat, conf, rx in _COMPILED:
        for m in rx.finditer(norm):
            s = m.group(0)
            excerpt = (s[:40] + "…") if len(s) > 40 else s
            hits.append(InjectionHit(code, cat, conf, (m.start(), m.end()), excerpt))
    return hits


def scan_codes(text: str) -> list[str]:
    """Backwards-compatible flat list of matched class codes (for callers that
    only want 'was anything seen')."""
    return [h.code for h in scan(text)]


def highest_confidence(hits: list[InjectionHit]) -> str | None:
    order = {"high": 3, "medium": 2, "low": 1}
    if not hits:
        return None
    return max(hits, key=lambda h: order[h.confidence]).confidence
