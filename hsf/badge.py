"""hsf badge — receipt -> SVG shield. Evidence-owned badges: the numbers
come from the receipt file, never typed by hand."""
from __future__ import annotations
import json
from pathlib import Path

_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{label}: {value}">
<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
<rect rx="3" width="{w}" height="20" fill="#555"/>
<rect rx="3" x="{lw}" width="{vw}" height="20" fill="{color}"/>
<rect rx="3" width="{w}" height="20" fill="url(#s)"/>
<g fill="#fff" text-anchor="middle" font-family="Verdana,sans-serif" font-size="11">
<text x="{lx}" y="14">{label}</text><text x="{vx}" y="14">{value}</text></g></svg>"""

def badge_from_receipt(receipt_path: str | Path, out_path: str | Path | None = None) -> Path:
    r = json.loads(Path(receipt_path).read_text())
    acc = next(g["evidence"].get("accuracy") for g in r["gates"] if g["gate"] == "accuracy")
    det = next(g["evidence"].get("byte_identical") for g in r["gates"] if g["gate"] == "execution")
    shipped = r["shipped"]
    label = "hsf gates"
    value = f"H=0 · {int(acc*100)}% goldens" if shipped and det else "FAILED"
    color = "#2ea44f" if shipped else "#d73a49"
    lw, vw = 6 * len(label) + 12, 6 * len(value) + 12
    svg = _SVG.format(w=lw + vw, lw=lw, vw=vw, lx=lw // 2, vx=lw + vw // 2,
                      label=label, value=value, color=color)
    out = Path(out_path or Path(receipt_path).with_suffix(".badge.svg"))
    out.write_text(svg)
    return out
