"""The manifest decoder shows real manifests, and keeps showing them.

`docs/guide/manifests.html` embeds the example manifests so the decoder can render them at their
true line numbers. Embedded text drifts from the files people run, and a confidently wrong
document outlives a missing one (CLAUDE.md, the prime directive) - so the embedding is generated
by `scripts/sync-manifest-decoder.py` and these tests fail when it is stale.

The dictionary is checked too: a decoder that colours a key it cannot explain, or explains a key
no manifest contains, is worse than one that does neither.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "guide" / "manifests.html"
DECODER = ROOT / "docs" / "assets" / "manifest-decoder.js"
SYNC = ROOT / "scripts" / "sync-manifest-decoder.py"


def embedded() -> dict[str, dict]:
    m = re.search(r'id="manifest-data">(.*?)</script>', PAGE.read_text(), re.DOTALL)
    assert m, "the decoder's manifest-data block is missing from the page"
    return json.loads(m.group(1))


def test_the_embedded_manifests_are_the_real_ones() -> None:
    """Byte for byte, trailing newline aside. This is the whole point of generating them."""
    for name, entry in embedded().items():
        path = ROOT / entry["path"]
        assert path.is_file(), f"{name}: {entry['path']} does not exist"
        assert entry["text"] == path.read_text().rstrip("\n"), (
            f"{name}: the page shows a different {entry['path']} than the repository has. "
            "Run `pixi run python scripts/sync-manifest-decoder.py`.")


def test_the_sync_script_reports_no_change() -> None:
    """The generator is idempotent, so a clean tree means the page is current. It exits 1 when
    it rewrote something, which is what makes it usable as a check."""
    out = subprocess.run([sys.executable, str(SYNC)], capture_output=True,
                         text=True, cwd=ROOT, check=False)
    assert out.returncode == 0, (
        f"the decoder is out of date - the sync script rewrote it: {out.stdout.strip()}")


def test_every_embedded_manifest_still_parses_as_yaml() -> None:
    for name, entry in embedded().items():
        try:
            yaml.safe_load(entry["text"])
        except yaml.YAMLError as e:
            pytest.fail(f"{name}: the embedded text is not valid YAML: {e}")


def dictionary_keys() -> set[str]:
    """The bare field names the decoder claims it can explain."""
    js = DECODER.read_text()
    body = js[js.index("var DICT = {"):js.index("/* ---- rendering")]
    return {k.split(".")[-1] for k in re.findall(r"^\s*'([a-z_.]+)':\s*\{", body, re.MULTILINE)}


def schema_keys() -> set[str]:
    """Every field name the manifest models declare, at any depth.

    Checking against the SCHEMA rather than against the examples is the stronger test: it still
    catches a typo or a field that was removed, but it does not force the decoder to stay silent
    about an optional field simply because no shipped example happens to set it.
    """
    from pydantic import BaseModel

    from stratum.manifest import models

    found: set[str] = set()
    for obj in vars(models).values():
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            for name, field in obj.model_fields.items():
                found.add(field.alias or name)
                found.add(name)
    return found


def test_the_dictionary_explains_nothing_that_does_not_exist() -> None:
    """A key in the dictionary that the models do not declare is a typo, or a field that was
    removed and took its documentation with it. Either way the decoder would colour something it
    could not explain, or explain something nobody can write."""
    stale = dictionary_keys() - schema_keys()
    assert not stale, (
        f"the decoder documents key(s) the manifest schema does not declare: {sorted(stale)}")


def test_the_dictionary_covers_what_the_examples_actually_use() -> None:
    """The other direction. A key a shipped manifest uses but the decoder cannot explain renders
    as plain text, which reads as "this one does not matter" rather than "nobody wrote it up"."""
    import yaml as _yaml

    used: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                used.add(str(k))
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for entry in embedded().values():
        walk(_yaml.safe_load(entry["text"]))

    # Only keys the SCHEMA declares count. A manifest also invents names - `mineral_1` is a
    # layer the author chose, `view_zenith` an alias they named - and the decoder is right not to
    # colour those: they mean whatever this manifest says they mean.
    content = {"source", "path", "key", "attributes", "band", "role", "method", "provider",
               "prefer", "wheel", "plugins"}
    missing = (used & schema_keys()) - dictionary_keys() - content
    assert not missing, (
        f"shipped manifests use schema key(s) the decoder cannot explain: {sorted(missing)}")


def test_the_page_loads_the_decoder_and_its_data() -> None:
    page = PAGE.read_text()
    assert 'id="manifest-data"' in page, "no data block"
    assert "assets/manifest-decoder.js" in page, "the page never loads the decoder"
    assert 'id="decoder"' in page, "the decoder has no root element to attach to"


def test_the_decoder_hard_codes_no_colour() -> None:
    """`assets/diagrams.js` states the rule for figures and it holds here too: the palette is
    read off the stylesheet so the page follows light/dark. A literal colour in the decoder
    would look wrong in one theme and nobody would notice until a screenshot."""
    hits = re.findall(r"#[0-9a-fA-F]{3,8}\b", DECODER.read_text())
    assert not hits, f"literal colour(s) in the decoder: {hits}; use a CSS custom property"
