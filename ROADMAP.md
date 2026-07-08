# Roadmap

## v0.3 (shipped in this publish set)

- `hsf aku`: export a seven-part Atomic Knowledge Unit bundle from a workflow
  spec: intent, procedure, tools, metadata, governance, continuations, and
  validators.
- Governance gradient classification: `human_controlled`, `supervised`, or
  `autonomous`, derived from validator coverage and shipped receipt history.
- `hsf topology`: validate workflow topology manifests for dangling links and
  cycles before agents rely on them.
- `hsf meter`: report per-module context tokens with exact/estimated
  provenance, then write those fields into compile receipts.
- AKU validator triad as an executable gate via
  `hsf aku --require-autonomous`.
- Worked end-to-end example path in `examples/end_to_end/`.

## v0.2 (shipped)

- `hsf init` scaffold, `hsf demo`, `hsf serve` (FastAPI), `hsf badge`
- Spec gallery: 5 domains, zero-code-change generality test

## v0.4 (help wanted)

- [ ] `hsf draft "<policy in English>"` - LLM drafts the spec YAML, which then
      walks the normal loader + gates (generation-plane only; good first issue
      for the prompt, the pipeline already exists)
- [ ] ed25519 signing via `cryptography` (interface is ready in `hsf/registry`)
- [ ] GitHub Action: run someone's specs through the four gates in their CI
- [ ] Temporal backend beyond the adapter stub
- [ ] TypeScript artifact target
- [ ] Nested/multi-branch workflow specs (sequential branches, sub-workflows)
- [ ] Activation policy: context-sensitive AKU availability by actor,
      environment, and incident status

## Principles that won't change

One model call per workflow type. H = 0 at runtime. No artifact ships ungated.
Claims trace to receipts, never prose.
