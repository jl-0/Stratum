#!/usr/bin/env python3
"""Repoint `<a class="src">` line anchors after code moves.

`tests/test_docs_links.py` fails when a documented line number no longer holds the symbol it
names, and its docstring is explicit that the fix is repointing the link rather than loosening
the check. Doing that by hand is fine once and tedious by the fourth time.

This only moves a link whose symbol has moved, finds the new line by searching the target file
for the same `data-sym`, and refuses to guess: a symbol that appears on several lines resolves to
the one nearest the current anchor, and one that appears nowhere is reported and left alone.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
BASE = "https://github.com/jl-0/Stratum/blob/main/"
LINK = re.compile(r'<a class="src"[^>]*?data-sym="([^"]*)"[^>]*?href="([^"]+)"'
                  r'|<a class="src"[^>]*?href="([^"]+)"[^>]*?data-sym="([^"]*)"')


def main() -> int:
    moved = unfixable = 0
    for page in sorted(DOCS.rglob("*.html")):
        text = page.read_text()
        out = text
        for m in LINK.finditer(text):
            sym, url = (m.group(1), m.group(2)) if m.group(2) else (m.group(4), m.group(3))
            sym = html.unescape(sym)
            if not url.startswith(BASE) or "#L" not in url:
                continue
            rel, _, frag = url[len(BASE):].partition("#")
            target = ROOT / rel
            if not target.exists():
                print(f"  {page.name}: {rel} does not exist"); unfixable += 1; continue
            current = int(frag.lstrip("L"))
            lines = target.read_text().splitlines()
            if current <= len(lines) and sym in lines[current - 1]:
                continue                                   # still correct
            hits = [i + 1 for i, line in enumerate(lines) if sym in line]
            if not hits:
                print(f"  {page.name}: {sym!r} is gone from {rel}"); unfixable += 1; continue
            best = min(hits, key=lambda n: abs(n - current))
            out = out.replace(f"{rel}#L{current}", f"{rel}#L{best}")
            print(f"  {page.name}: {sym!r} {current} -> {best}  ({rel})")
            moved += 1
        if out != text:
            page.write_text(out)
    print(f"repointed {moved}; {unfixable} need a human")
    return 1 if unfixable else 0


if __name__ == "__main__":
    sys.exit(main())
