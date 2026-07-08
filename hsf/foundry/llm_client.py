"""Provider-agnostic LLM client (generation plane ONLY).

This module must never be importable from hsf.runtime (see
tests/test_foundry.py::test_runtime_package_cannot_import_foundry).
"""
from __future__ import annotations
import os

class LLMUnavailable(RuntimeError):
    pass

CALL_COUNTER = {"calls": 0}

def complete(system: str, user: str, model: str = "claude-sonnet-4-6") -> str:
    CALL_COUNTER["calls"] += 1
    try:
        import anthropic  # optional extra: pip install harness-factory[llm]
    except ImportError as e:
        raise LLMUnavailable("anthropic not installed — use engine='template' or pip install .[llm]") from e
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY unset — use engine='template'")
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=model, max_tokens=4000, temperature=0,
        system=system, messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
