"""Quarantined extractor (Dual-LLM pattern): schema-locked JSON, no tools,
no authority over the workflow, retry-once then HUMAN_REVIEW.

FixtureExtractor serves tests/goldens and offline use. AnthropicExtractor
is the optional real one, holding ONLY HSF_EXTRACTOR_KEY (never the
generation-plane credential)."""
from __future__ import annotations
import json, os

EXTRACTOR_PROMPT_VERSION = "clinical_v1"

class FixtureExtractor:
    def __init__(self, fields: dict):
        self.fields = fields
    async def extract(self, schema: dict, text: str) -> dict | None:
        return {k: self.fields.get(k) for k in schema}

class AnthropicExtractor:
    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.model = model
    async def extract(self, schema: dict, text: str) -> dict | None:
        try:
            import anthropic
        except ImportError:
            return None
        key = os.environ.get("HSF_EXTRACTOR_KEY")
        if not key:
            return None
        client = anthropic.Anthropic(api_key=key)
        from pathlib import Path
        prompt = (Path(__file__).parent / "extractor_prompts" / f"{EXTRACTOR_PROMPT_VERSION}.txt").read_text()
        for _ in range(2):  # retry-once-on-invalid-JSON
            resp = client.messages.create(
                model=self.model, max_tokens=500, temperature=0,
                system=prompt, messages=[{"role": "user",
                    "content": f"SCHEMA:\n{json.dumps(schema)}\n\nNOTE:\n{text[:8000]}"}])
            raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
        return None  # -> HUMAN_REVIEW via sandwich
