"""The Developer section makes structural claims; these keep them true.

`tests/test_docs_links.py` catches a code link that has drifted onto the wrong line. It cannot
catch the other way a structural page rots: a module, a package or a layer that was added and
documented nowhere breaks no link at all, so nothing fails and the map quietly stops being a map.

The site has no build step and must not acquire one (CLAUDE.md, "Working on the design site"), so
these pages are hand-written and it is these tests, not a generator, that keep them honest.

Two of the three assertions are about the source tree rather than the docs - the framework/plugin
boundary and the acyclic package graph. `docs/developer/codebase.html` states both as facts, and a
documented invariant nobody enforces is a documented aspiration.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
MAP = ROOT / "docs" / "developer" / "codebase.html"
MODULES = sorted(SRC.rglob("*.py"))


def unit(path: Path) -> str:
    """How `codebase.html` names the thing this module belongs to: the subpackage directory it
    lives in (`access/`), or the module's own file name when it sits at the package root."""
    rel = path.relative_to(SRC)
    if rel.parts[0] == "stratum_emit":
        return "stratum_emit/"
    return f"{rel.parts[1]}/" if len(rel.parts) > 2 else rel.parts[-1]


def package(module: str) -> str:
    """`stratum.access.store` -> `stratum.access`; the whole EMIT package is one node."""
    parts = module.split(".")
    if parts[0] == "stratum_emit":
        return "stratum_emit"
    return ".".join(parts[:2]) if len(parts) > 1 else "stratum"


def test_every_module_is_on_the_codebase_map() -> None:
    """A package added to `src/` without a row on the map. Failing here is a prompt to write the
    row, not to loosen the check: an undocumented package is exactly what the page exists to
    prevent."""
    page = MAP.read_text()
    missing = sorted({u for m in MODULES if (u := unit(m)) != "__init__.py" and u not in page})
    assert not missing, (f"{MAP.name} documents no such module(s): {missing}; "
                         "add a row to the module-by-module table")


def repo_modules() -> list[str]:
    """Every Python file in the working tree, repo-relative. Not just `src/`: the map cites test
    modules too, and a caption naming a test that has been renamed rots exactly the same way."""
    skip = (".pixi", ".git", "vendor", "__pycache__", ".ruff_cache", ".pytest_cache")
    return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
            if not any(part in skip for part in p.relative_to(ROOT).parts)]


def test_the_codebase_map_names_no_module_that_is_gone() -> None:
    """The other direction: a module renamed or deleted while the map still cites it. Only
    `<code>` spans ending in `.py` are considered - prose and file-format names are not paths."""
    tokens = {t for t in re.findall(r"<code>([A-Za-z0-9_./]+\.py)</code>", MAP.read_text())}
    known = repo_modules()
    gone = sorted(t for t in tokens if not any(k.endswith(t) for k in known))
    assert not gone, f"{MAP.name} names module(s) that no longer exist: {gone}; repoint them"


def imports_of(path: Path) -> set[str]:
    """Every in-repo module this file imports, by dotted name."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        out |= {n for n in names if n.split(".")[0] in ("stratum", "stratum_emit")}
    return out


def test_the_framework_never_imports_the_plugin_package() -> None:
    """`src/stratum/` is instrument-agnostic and `src/stratum_emit/` is where EMIT lives; the two
    meet through entry points at run time, never through an import. One `import stratum_emit` in
    the framework is all it takes for Stratum to stop being re-targetable."""
    offenders = sorted(
        f"{m.relative_to(ROOT)} imports {i}"
        for m in MODULES if m.relative_to(SRC).parts[0] == "stratum"
        for i in imports_of(m) if i.split(".")[0] == "stratum_emit")
    assert not offenders, ("the framework must not know about the EMIT plugin package "
                           f"(docs/developer/codebase.html section 1): {offenders}")


def test_the_package_import_graph_is_acyclic() -> None:
    """The layer table on the codebase map is only meaningful if the graph really is a DAG. A
    cycle means two packages have grown a mutual dependency and one of them owns something that
    belongs to the other - or to `stratum.types`."""
    edges: dict[str, set[str]] = defaultdict(set)
    for path in MODULES:
        rel = path.relative_to(SRC).with_suffix("")
        parts = rel.parts[:-1] if rel.parts[-1] == "__init__" else rel.parts
        here = package(".".join(parts))
        edges[here] |= {p for i in imports_of(path) if (p := package(i)) != here}

    state: dict[str, int] = {}
    cycles: list[list[str]] = []

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges[node]):
            if state.get(nxt) == 1:
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif not state.get(nxt):
                visit(nxt, stack)
        stack.pop()
        state[node] = 2

    for node in sorted(edges):
        if not state.get(node):
            visit(node, [])
    assert not cycles, f"import cycle between packages: {[' -> '.join(c) for c in cycles]}"


@pytest.mark.parametrize("page", sorted((ROOT / "docs" / "developer").glob("*.html")))
def test_developer_pages_are_registered_in_the_nav(page: Path) -> None:
    """`data-page` must match an id in `PAGES` (stratum.js owns the only copy of the nav model),
    and `data-root` must be `../` one level down. Both fail silently in a browser: a wrong
    `data-page` loses the highlight, a wrong `data-root` breaks every link on the page."""
    body = re.search(r'<body([^>]*)>', page.read_text())
    assert body, f"{page.name} has no <body> tag"
    attrs = body.group(1)
    data_page = re.search(r'data-page="([^"]+)"', attrs)
    assert data_page, f"{page.name} sets no data-page"
    assert 'data-root="../"' in attrs, f"{page.name} is one level down; data-root must be ../"
    nav = (ROOT / "docs" / "assets" / "stratum.js").read_text()
    assert f"id: '{data_page.group(1)}'" in nav, (
        f"{page.name} declares data-page {data_page.group(1)!r}, which is not an id in PAGES")
