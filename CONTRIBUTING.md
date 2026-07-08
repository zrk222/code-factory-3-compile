# Contributing

- Every change ships with tests; gate behavior changes require updated
  vulnerable fixtures or goldens proving the new behavior.
- No hand-copied metrics anywhere: numbers in docs must trace to a receipt
  or a test assertion.
- New workflow types are specs + goldens only. If you need a code change to
  add a workflow, that's a bug in the factory — file it as one.
- Public benchmark discipline: never tune gates or the compiler on the same
  fixture set used for a public claim.
