"""Reference activities for the GLP-1 example (synthetic only — PRD NG5)."""
from __future__ import annotations

def extract_clinical_data(text: str) -> dict:
    """Placeholder deterministic activity; real extraction runs via the Safety Sandwich."""
    return {"length": len(text)}

def flag_for_review(reason: str) -> dict:
    return {"flagged": True, "reason": reason}
