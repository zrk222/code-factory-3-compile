"""Token meter utilities used by receipts and CLI reports.

The meter prefers a real tokenizer when available. If the optional tokenizer is
missing, the report is explicit about using an estimate so savings claims do
not masquerade as measured evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    method: str
    exact: bool
    encoding: str


def count_tokens(text: str, encoding_name: str = "cl100k_base") -> TokenCount:
    try:
        import tiktoken  # type: ignore
    except ImportError:
        return TokenCount(
            tokens=math.ceil(len(text) / 4),
            method="chars_per_token_estimate",
            exact=False,
            encoding=encoding_name,
        )

    encoding = tiktoken.get_encoding(encoding_name)
    return TokenCount(
        tokens=len(encoding.encode(text)),
        method="tiktoken",
        exact=True,
        encoding=encoding_name,
    )


def module_token_report(parts: list[tuple[str, str]], encoding_name: str = "cl100k_base") -> dict:
    modules: dict[str, dict[str, object]] = {}
    total_tokens = 0
    exact = True
    methods: set[str] = set()

    for name, text in parts:
        module = name.split("/", 1)[0]
        counted = count_tokens(text, encoding_name)
        if module not in modules:
            modules[module] = {"tokens": 0, "files": 0, "bytes": 0, "exact": True, "method": counted.method}
        bucket = modules[module]
        bucket["tokens"] = int(bucket["tokens"]) + counted.tokens
        bucket["files"] = int(bucket["files"]) + 1
        bucket["bytes"] = int(bucket["bytes"]) + len(text.encode("utf-8"))
        bucket["exact"] = bool(bucket["exact"]) and counted.exact
        bucket["method"] = counted.method if bucket["method"] == counted.method else "mixed"
        total_tokens += counted.tokens
        exact = exact and counted.exact
        methods.add(counted.method)

    return {
        "total_tokens": total_tokens,
        "exact": exact,
        "method": next(iter(methods)) if len(methods) == 1 else "mixed",
        "encoding": encoding_name,
        "modules": dict(sorted(modules.items())),
    }


def context_token_report(max_tokens: int = 32_000, encoding_name: str = "cl100k_base") -> dict:
    from hsf.context.assembler import assemble_context

    context = assemble_context(max_tokens=max_tokens)
    report = module_token_report(context["parts"], encoding_name)
    report["pruned"] = context["pruned"]
    report["budget_tokens"] = max_tokens
    return report
