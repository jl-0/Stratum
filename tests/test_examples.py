"""Every example manifest keeps validating, and the demonstration contenders stay comparable.

An example is documentation people copy. One that names a plugin or parameter that no longer
exists, or that has drifted from its siblings, misleads more than a missing one, so each is
checked with `validate_static` (no data, no login), and that check builds every plugin the manifest
names with its params. A misspelt or retired scorer, mask or parameter fails here.

The demonstration sites (`examples/emit-cm-demo-sites/`) are built to be flipped between band for
band in a viewer, so what they must share is pinned too: grid, sites, year, filters, masks and the
delivered band names. Contender B claims to be the external aggregator's defaults, and C claims to
change exactly three of B's settings; both claims are checked against `ModeGatedSelect`. The
confirmed-vote contender keeps A's scorer and changes the epoch and the reduction; the
score-weighted one changes exactly B's vote weighting.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml
from stratum_emit.scorers import ModeGatedSelect

from stratum.manifest import load_manifest
from stratum.manifest.validate import validate_static

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*/manifest*.yaml"))
DEMO = ROOT / "examples" / "emit-cm-demo-sites"

#: the full product as designed: refused on purpose, it names features not yet built (see its header)
DESIGN_ONLY = {"emit-critical-minerals/manifest.yaml"}


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT / "examples"))


def _raw(name: str) -> dict:
    return yaml.safe_load((DEMO / f"manifest-{name}.yaml").read_text())


def test_the_examples_are_found() -> None:
    names = {_rel(p) for p in EXAMPLES}
    assert "emit-cm-demo-sites/manifest-mode-gated.yaml" in names
    assert "emit-cmr-cuprite/manifest-mode-gated.yaml" in names


@pytest.mark.parametrize("path", EXAMPLES, ids=_rel)
def test_example_manifest_validates(path: Path) -> None:
    errors = validate_static(load_manifest(path))
    if _rel(path) in DESIGN_ONLY:
        assert errors, f"{_rel(path)} is a design manifest and should list what is not built"
    else:
        assert errors == [], f"{_rel(path)}: {errors}"


# ------------------------------------------------------------------- demonstration sites
CONTENDERS = ("baseline", "mode-gated", "assistant-pick", "confirmed", "score-weighted")
SHARED = ("grid", "aoi", "aux", "granule_filter", "pixel_mask")


@pytest.mark.parametrize("key", SHARED)
def test_contenders_share_everything_but_selection(key: str) -> None:
    base = _raw("baseline")[key]
    for name in CONTENDERS[1:]:
        assert _raw(name)[key] == base, f"manifest-{name}.yaml: {key} differs from A"


def test_contenders_share_one_index() -> None:
    """One `./index` serves all three only while the source and its patterns are identical. B and
    C read more roles (MINUNCERT, the cloud-probability band) but never a different granule set."""
    inputs = {n: _raw(n)["inputs"] for n in CONTENDERS}
    for name in CONTENDERS[1:]:
        for key in ("index_location", "source"):
            assert inputs[name][key] == inputs["baseline"][key], f"manifest-{name}.yaml: {key}"
        assert inputs["baseline"]["roles"].items() <= inputs[name]["roles"].items()
    assert inputs["mode-gated"] == inputs["assistant-pick"] == inputs["score-weighted"]


def test_contenders_use_the_l3_reflectance_grid() -> None:
    g = _raw("baseline")["grid"]
    res = g["resolution"][0]
    assert g["crs"] == "EPSG:4326" and g["resolution"] == [0.00055, -0.00055]
    # cell centres on whole multiples of 0.00055 deg, so edges sit half a cell off them
    assert g["origin"] == [-res / 2, -res / 2]
    assert round(g["tile_size"] / res, 9) == 1000


def test_contenders_cover_one_year_over_the_four_sites() -> None:
    zones = {"cuprite-nv", "mountain-pass-ca", "bingham-canyon-amd", "mojave-l2b-tutorial"}
    registry = yaml.safe_load((ROOT / "examples" / "zones.yaml").read_text())
    names = registry.get("zones", registry)
    for name in CONTENDERS:
        doc = _raw(name)
        assert set(doc["aoi"]["zones"]) == zones
        assert zones <= set(names), "a site is missing from examples/zones.yaml"
        assert str(doc["time"]["start"]) == "2025-01-01" and doc["time"]["deliver"] == "P1Y"


def test_contenders_deliver_the_same_bands() -> None:
    """A flips with B and C band for band; A adds acquired_2 (its groups vote separately) and
    B/C add the scorer's diagnostics."""
    common = {"mineral_1", "mineral_2", "depth_1", "depth_2", "acquired_1", "bare", "pv", "npv"}
    diagnostics = {"mode_count", "n_valid", "agreement", "selection_penalty"}
    layers = {n: set(_raw(n)["snapshot"]["layers"]) for n in CONTENDERS}
    assert layers["baseline"] == common | {"acquired_2"}
    assert layers["mode-gated"] == layers["assistant-pick"] == layers["score-weighted"] \
        == common | diagnostics


def test_confirmed_keeps_a_scorer_and_delivers_through_the_reducer() -> None:
    """It differs from A in the epoch and the reduction only; its plugin delivers the mineral,
    depth and selected-granule bands under A's names."""
    a, d = _raw("baseline"), _raw("confirmed")
    assert d["scorer"] == a["scorer"] and d["time"]["epoch"] == "P1D"
    assert d["reducer"]["ref"] == "confirmed_mineral_vote"
    from stratum_emit.reducers import ConfirmedMineralVote
    bands = {b.name for b in ConfirmedMineralVote(**d["reducer"]["params"]).outputs}
    assert {"mineral_1", "mineral_2", "depth_1", "depth_2", "acquired_1", "acquired_2"} <= bands
    # and its mineral bands render like everyone else's: a render names a DELIVERED band
    m = load_manifest(DEMO / "manifest-confirmed.yaml")
    assert {"mineral_1", "mineral_2"} <= set(m.outputs.render) and not validate_static(m)


def test_contenders_share_one_colour_table() -> None:
    """Every contender colours a class the same way, so their maps compare cell for cell: one
    `outputs.colors` file, the 1995 USGS Cuprite groups, and nothing per-render that overrides it."""
    for name in CONTENDERS:
        out = _raw(name)["outputs"]
        assert out["colors"] == "@ref:colors-usgs-1995.yaml", name
        assert all("colors" not in r for r in out["render"].values()), name
    colours = load_manifest(DEMO / "manifest-baseline.yaml").output_colors()
    assert len(colours) > 100 and colours["Hematite.02+Quartz.98 GDS76 W1R1Ha"] == (235, 0, 180)


def test_score_weighted_changes_only_bs_vote_weighting() -> None:
    b = _raw("mode-gated")["scorer"]["params"]
    e = _raw("score-weighted")["scorer"]["params"]
    changed = {k for k in b.keys() | e.keys() if b.get(k) != e.get(k)}
    assert changed == {"vote_weight", "vote_power"} and e["vote_weight"] == "score"


def test_contender_scorers() -> None:
    assert _raw("baseline")["scorer"]["ref"] == "low_vegetation_nadir"
    assert _raw("baseline")["time"]["epoch"] == "P1W"
    for name in ("mode-gated", "assistant-pick", "score-weighted"):
        assert load_manifest(DEMO / f"manifest-{name}.yaml").scorer.ref == "mode_gated_select"
        assert _raw(name)["time"]["epoch"] == "P1Y"


def test_b_is_the_mode_gated_scorers_defaults() -> None:
    """B is 'the external aggregator's defaults', and so is ModeGatedSelect() - one changes,
    the other must follow. `layers`/`aliases` name this directory's roles, so they are skipped."""
    params = _raw("mode-gated")["scorer"]["params"]
    built, default = ModeGatedSelect(**params), ModeGatedSelect()
    for attr in ("key", "gate", "normalize", "weights", "bounds"):
        assert getattr(built, attr) == getattr(default, attr), attr
    signature = inspect.signature(ModeGatedSelect).parameters
    for key, value in params.items():
        if key in ("layers", "aliases", "weights", "bounds", "ignore", "g2_mask_classes"):
            continue
        assert value == signature[key].default, f"{key}: {value!r} is not the default"


def test_c_changes_exactly_three_of_bs_settings() -> None:
    b = _raw("mode-gated")["scorer"]["params"]
    c = _raw("assistant-pick")["scorer"]["params"]
    changed = {k for k in b.keys() | c.keys() if b.get(k) != c.get(k)}
    assert changed == {"key", "gate", "normalize"}
    assert (c["key"], c["gate"], c["normalize"]) == ("independent", "soft", "rank")
