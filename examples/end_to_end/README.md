# End-to-End Factory Walkthrough

This is the smallest honest sales demo for HSF: one real workflow goes from
spec to gates to signed artifact to receipt to AKU.

Run from the repository root:

```bash
python -m pip install -e ".[dev]"
hsf validate specs/refund_review.yaml
hsf compile specs/refund_review.yaml
hsf goldens registry_store/refund_review-*.py refund_review
hsf aku specs/refund_review.yaml --receipt receipts/refund_review-*.receipt.json -o examples/end_to_end/refund_review.aku.json
hsf meter
```

PowerShell users should resolve the generated files first:

```powershell
python -m pip install -e ".[dev]"
hsf validate specs/refund_review.yaml
hsf compile specs/refund_review.yaml

$artifact = Get-ChildItem -LiteralPath registry_store -Filter "refund_review-*.py" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty FullName
hsf goldens $artifact refund_review

$receipt = Get-ChildItem -LiteralPath receipts -Filter "refund_review-*.receipt.json" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty FullName
hsf aku specs/refund_review.yaml --receipt $receipt --require-autonomous -o examples/end_to_end/refund_review.aku.json
hsf meter
```

What to look for:

- The compile receipt contains `token_meter.compile`, `token_meter.runtime`,
  `token_meter.context_modules`, and `token_meter.savings`.
- Template-engine compile has `compile.model_calls = 0`.
- Runtime has `tokens_per_tx = 0`.
- `hsf aku --require-autonomous` only passes when the receipt proves all four
  factory gates passed and `shipped` is true.
- `hsf meter` reports token density by context module and says whether the
  count was exact (`tiktoken`) or estimated (`chars_per_token_estimate`).

This example is deliberately a normal business workflow, not a benchmark-only
fixture. It is meant to answer the practical question: "Can I ship my policy as
deterministic code with receipts?"
