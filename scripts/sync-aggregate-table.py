#!/usr/bin/env python
"""Write the `aggregate` vocabulary into `docs/reference/manifest.html`.

A manifest author cannot know what to type into `aggregate` without reading
`stratum/manifest/models.py`, which is not a reasonable thing to ask. The reference showed
examples and never the vocabulary.

The table is DERIVED, not transcribed: which parameters a method takes is imperative code in
`LayerModel._by_kind`, so this probes the model - it builds a layer for every (kind, method) and
for every candidate parameter, and records what the validator actually accepted. Transcribing the
rules by hand would put a second copy of them in a file nobody runs.

    pixi run python scripts/sync-aggregate-table.py

Exits 1 when it changed something, so it is usable as a check.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "reference" / "manifest.html"
BEGIN = "<!-- BEGIN generated: aggregate vocabulary -->"
END = "<!-- END generated: aggregate vocabulary -->"

#: every parameter any method might take, with a value that is valid in isolation
CANDIDATES: dict[str, Any] = {
    "min_count": 2,
    "ignore": ["none"],
    "tie_break": "earliest",
    "p": 50.0,
    "unc": "other_layer",
    "conditional_on": "other_layer",
    "spread": "iqr",
}

#: one line on what each method does, for the column a probe cannot produce
MEANING = {
    "vote": "The modal class across epochs. The mineral-map default.",
    "best": "The value from the single highest-scoring epoch.",
    "none": "Carried in the snapshot, never delivered as a product band.",
    "median": "The middle value. Robust to one bad epoch.",
    "mean": "The arithmetic mean.",
    "min": "The smallest value.",
    "max": "The largest value.",
    "percentile": "The p-th percentile.",
    "score_weighted": "A mean weighted by each epoch's winning score.",
    "inverse_variance": "A mean weighted by 1/uncertainty&sup2;, from the layer <code>unc</code> names.",
}


def build(kind: str, method: str, extra: dict[str, Any] | None = None) -> None:
    """Construct one layer, or raise ValidationError."""
    from stratum.manifest.models import LayerModel

    agg: dict[str, Any] = {"method": method, **(extra or {})}
    layer: dict[str, Any] = {"kind": kind, "source": "src", "aggregate": agg}
    if kind == "categorical":
        layer["classes"] = "source"
    LayerModel(**layer)


def probe() -> dict[str, dict[str, Any]]:
    """Ask the model what each method accepts, in two passes.

    One pass is not enough: when a method has a REQUIRED parameter, every other parameter also
    fails in isolation - `inverse_variance` without `unc` is invalid whatever else you add - and
    the optional ones would be under-reported. So find what is required first, then probe the
    rest with the required ones already in place.
    """
    from stratum.manifest.models import CATEGORICAL_METHODS, CONTINUOUS_METHODS

    out: dict[str, dict[str, Any]] = {}
    for kind, methods in (("categorical", CATEGORICAL_METHODS), ("continuous", CONTINUOUS_METHODS)):
        for method in sorted(methods):
            entry = out.setdefault(method, {"kinds": [], "takes": set(), "needs": set()})
            entry["kinds"].append(kind)

            bare_ok = True
            try:
                build(kind, method)
            except ValidationError:
                bare_ok = False

            # pass 1: what makes the bare form valid?
            needs: dict[str, Any] = {}
            if not bare_ok:
                for name, value in CANDIDATES.items():
                    try:
                        build(kind, method, {name: value})
                    except ValidationError:
                        continue
                    needs[name] = value
            entry["needs"].update(needs)

            # pass 2: what else does it accept, with those in place?
            for name, value in CANDIDATES.items():
                if name in needs:
                    entry["takes"].add(name)
                    continue
                try:
                    build(kind, method, {**needs, name: value})
                except ValidationError:
                    continue
                entry["takes"].add(name)
    return out


def render(table: dict[str, dict[str, Any]]) -> str:
    rows = []
    for method in sorted(table, key=lambda m: (table[m]["kinds"], m)):
        e = table[method]
        needs = sorted(e["needs"])
        takes = sorted(e["takes"] - e["needs"])
        req = "".join(f"<code>{p}</code> " for p in needs) or "&mdash;"
        opt = "".join(f"<code>{p}</code> " for p in takes) or "&mdash;"
        rows.append(
            f'<tr><td><code>{method}</code></td>'
            f'<td>{" / ".join(e["kinds"])}</td>'
            f'<td>{MEANING.get(method, "")}</td>'
            f'<td>{req.strip()}</td><td>{opt.strip()}</td></tr>')
    return (
        "\n<div class=\"tw\">\n<table>\n"
        "<thead><tr><th>method</th><th>layer kind</th><th>what it does</th>"
        "<th>requires</th><th>also takes</th></tr></thead>\n<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody>\n</table>\n</div>\n"
        "<p class=\"gen\">Derived from <code>stratum.manifest.models</code> by "
        "<code>scripts/sync-aggregate-table.py</code>: every cell is what the validator actually "
        "accepted, not a transcription of it. A parameter a method does not take is a load-time "
        "error naming the method.</p>\n")


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    text = PAGE.read_text()
    start = text.find(BEGIN)
    end = text.find(END)
    if start == -1 or end == -1:
        sys.exit(f"{PAGE}: no {BEGIN} / {END} markers to fill between")
    fresh = render(probe())
    current = text[start + len(BEGIN):end]
    if current == fresh:
        print("aggregate table is current")
        return 0
    PAGE.write_text(text[:start + len(BEGIN)] + fresh + text[end:])
    print(f"updated {PAGE.relative_to(ROOT)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
