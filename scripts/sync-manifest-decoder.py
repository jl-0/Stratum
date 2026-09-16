#!/usr/bin/env python
"""Write the example manifests into the decoder on `docs/guide/manifests.html`.

The decoder shows real manifests at their true line numbers. Embedding them means
they can drift from the files people actually run, which would make the page a
confident description of something that does not exist - the failure mode
`docs/README.md` warns about for every other kind of doc.

So the embedding is generated, never hand-edited, and `tests/test_docs_manifests.py`
fails when the page and the files disagree. Run this after editing an example:

    pixi run python scripts/sync-manifest-decoder.py

Exits 1 when it changed something, so it is usable as a check in a pipeline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "guide" / "manifests.html"
BEGIN = '<script type="application/json" id="manifest-data">'
END = "</script>"

#: name -> (path relative to the repo root, button label, one line under the filename)
MANIFESTS: dict[str, tuple[str, str, str]] = {
    "bare-earth": (
        "examples/emit-cmr-cuprite/manifest-bare-earth.yaml",
        "Cuprite, bare earth",
        "Every input path at once: an ortho-native role, an aux source, a full year of epochs.",
    ),
    "joint": (
        "examples/emit-cmr-cuprite/manifest-joint.yaml",
        "Cuprite, joint minerals",
        "The only manifest that names a `reducer`: two mineral groups combined by a plugin.",
    ),
    "nevada": (
        "examples/emit-cmr-nevada/manifest.yaml",
        "Nevada, one tile",
        "The smallest runnable manifest. One tile, one scorer, no aux.",
    ),
    "critical-minerals": (
        "examples/emit-critical-minerals/manifest.yaml",
        "Critical Minerals (design)",
        "The full product as designed. Not runnable - `stratum validate` lists what is missing.",
    ),
}


def payload() -> str:
    out = {}
    for name, (rel, label, note) in MANIFESTS.items():
        path = ROOT / rel
        if not path.is_file():
            sys.exit(f"{rel}: not found; fix scripts/sync-manifest-decoder.py")
        out[name] = {"path": rel, "label": label, "note": note,
                     "text": path.read_text().rstrip("\n")}
    # separators without spaces keeps the blob compact; the page is served as-is
    return json.dumps(out, indent=None, separators=(",", ":"))


def main() -> int:
    text = PAGE.read_text()
    start = text.find(BEGIN)
    if start == -1:
        sys.exit(f"{PAGE}: no {BEGIN!r} block to fill")
    open_end = start + len(BEGIN)
    close = text.find(END, open_end)
    if close == -1:
        sys.exit(f"{PAGE}: the manifest-data script is never closed")

    current = text[open_end:close]
    fresh = payload()
    if current == fresh:
        print("manifest decoder is current")
        return 0
    PAGE.write_text(text[:open_end] + fresh + text[close:])
    print(f"updated {PAGE.relative_to(ROOT)} with {len(MANIFESTS)} manifest(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
