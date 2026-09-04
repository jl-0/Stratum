"""Every code link on the site still points at the line it names.

`docs/guide/algorithms.html` documents the pipeline by linking each step to the line that
implements it, and a line number rots the moment anyone inserts code above it - which happened
within an hour of the page being written. Each `<a class="src">` therefore carries `data-sym`,
a distinctive fragment of the line it claims, and this test re-reads the file.

Fixing a failure is repointing the link, never loosening the assertion: a doc that links the
wrong line is worse than one that links nothing.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
ROOT = DOCS.parent
BASES = {
    "https://github.com/jl-0/Stratum/blob/main/": ROOT,
    "https://github.com/emit-sds/SpectralUtil/blob/590dafd/": ROOT / "vendor" / "SpectralUtil",
}
LINK = re.compile(r'<a class="src"[^>]*?data-sym="([^"]*)"[^>]*?href="([^"]+)"'
                  r'|<a class="src"[^>]*?href="([^"]+)"[^>]*?data-sym="([^"]*)"')


def anchors() -> list[tuple[Path, str, str]]:
    out = []
    for page in sorted(DOCS.rglob("*.html")):
        for m in LINK.finditer(page.read_text()):
            sym, url = (m.group(1), m.group(2)) if m.group(2) else (m.group(4), m.group(3))
            out.append((page, html.unescape(sym), url))
    return out


def resolve(url: str) -> tuple[Path, int] | None:
    for base, root in BASES.items():
        if url.startswith(base):
            path, _, frag = url[len(base):].partition("#")
            return root / path, int(frag.lstrip("L")) if frag.startswith("L") else 0
    raise AssertionError(f"code link {url} is not under a known repository base {list(BASES)}")


@pytest.mark.parametrize("page, sym, url", anchors(),
                         ids=lambda v: v if isinstance(v, str) else str(v)[-40:])
def test_code_link_points_at_the_line_it_names(page: Path, sym: str, url: str) -> None:
    target, line_no = resolve(url)
    if not target.exists():
        if BASES["https://github.com/emit-sds/SpectralUtil/blob/590dafd/"] in target.parents:
            pytest.skip("vendor/SpectralUtil is not checked out")
        pytest.fail(f"{page.name}: {url} names {target}, which does not exist")
    if not line_no:
        return
    lines = target.read_text().splitlines()
    assert line_no <= len(lines), f"{page.name}: {url} is past the end of {target.name}"
    assert sym in lines[line_no - 1], (
        f"{page.name}: {url} claims {sym!r} but line {line_no} of {target.name} is "
        f"{lines[line_no - 1].strip()!r}; repoint the link")


def test_the_page_actually_carries_links() -> None:
    """A regex that silently matches nothing would make every assertion above vacuous."""
    assert len(anchors()) >= 30
