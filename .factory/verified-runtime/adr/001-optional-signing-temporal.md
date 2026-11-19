# ADR 001: Optional public-key signing and Temporal SDK

Status: accepted within the user-authorized verified-runtime upgrade.

Use `cryptography>=43` for Ed25519 signatures and `temporalio>=1.36.1,<2`
for actual Temporal workflows, activities, replay and worker recovery. Both are
optional extras; Temporal also requires signing. Preserve lazy imports and the
existing base installation. CI's controlled Temporal job pins SDK 1.36.1.

The existing local HMAC mode cannot establish pinned public-key trust; a mock
durable adapter cannot establish SDK execution or replay. Reusing maintained
libraries avoids implementing cryptography or the Temporal protocol locally.

Dependency authorization is part of the requested signed deterministic Temporal
upgrade. This decision permits isolated test installation and repository metadata
only; it grants no global install, release, deployment, credential or external
effect authority. Controlled server evidence is separate from production proof.
