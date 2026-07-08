# glp1_review golden dataset
40 synthetic cases (seeded, reproducible via `generate.py`). No real PHI —
names are from a fixed synthetic list; IDs are sequential fakes.
Buckets: 10 T2D approvals · 10 BMI approvals · 10 denials · 5 out-of-bounds
A1c → HUMAN_REVIEW · 5 adversarial prompt-injection notes (decision must be
unaffected; injection is flagged in the audit log, not obeyed).
