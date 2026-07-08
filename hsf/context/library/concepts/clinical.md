# Clinical review semantics (Layer 1 — Concepts)
- A prior-authorization decision is APPROVED, DENIED, or HUMAN_REVIEW. Nothing else.
- Out-of-range physiological values are never auto-decided; they route to HUMAN_REVIEW.
- Decision rules are ordered, first-match-wins, and must be exhaustive.
- Extraction is probabilistic; decision logic is deterministic. Never blend the two.
