from __future__ import annotations

from stratum_emit.scorers import MinViewZenith

from stratum.plugins import resolve


def test_module_class_reference_resolves_without_registration():
    assert resolve("scorer", "stratum_emit.scorers:MinViewZenith") is MinViewZenith


def test_max_band_depth_needs_only_the_depth_role():
    import numpy as np
    from stratum_emit.scorers import MaxBandDepth

    s = MaxBandDepth()
    assert s.required_roles == ("mineral_depth",) and s.capability == "streaming"

    class Obs:
        def __getitem__(self, alias):
            assert alias == "mineral_depth"
            return np.array([[0.1, 0.4]])

    out = s.score(Obs(), None)
    assert out.dtype == np.float32 and out[0, 1] > out[0, 0]


def test_prefer_bare_earth_ranks_cover_and_rejects_what_it_cannot_use():
    """04 section 4: the scorer RANKS between the two thresholds and returns NaN below the
    lower one, because "unusable" and "merely worse" are different statements."""
    import numpy as np
    from stratum_emit.scorers import PreferBareEarth

    s = PreferBareEarth()
    assert s.required_roles == ("frcov", "solar_zenith") and s.required_aux == ()

    class Obs:
        def __init__(self, soil, sun):
            self.bands = {"frcov": np.asarray(soil, dtype="float32"),
                          "solar_zenith": np.asarray(sun, dtype="float32")}

        def __getitem__(self, alias):
            return self.bands[alias]

    # cover ranks: more bare ground wins, and the 0.65-0.80 band still ranks rather than dying
    out = s.score(Obs([0.95, 0.85, 0.70, 0.60], [30.0] * 4), None)
    assert out[0] > out[1] > out[2], "more bare soil must score higher"
    assert np.isnan(out[3]), "below hard_floor the retrieval is unusable, not merely worse"

    # illumination breaks ties among comparable cover, and never outranks cover
    tie = s.score(Obs([0.9, 0.9], [10.0, 60.0]), None)
    assert tie[0] > tie[1], "the better-lit look of equal ground wins"
    beats = s.score(Obs([0.95, 0.70], [70.0, 0.0]), None)
    assert beats[0] > beats[1], "illumination must not outrank cover"

    # a sun below the horizon cannot light the surface
    assert np.isnan(s.score(Obs([0.9], [85.0]), None)[0])

    # sun_weight 0 is the pure cover ranking
    plain = PreferBareEarth(sun_weight=0.0).score(Obs([0.9, 0.8], [10.0, 60.0]), None)
    assert plain[0] > plain[1] and np.allclose(plain, [0.9, 0.8])


def test_prefer_bare_earth_refuses_thresholds_that_cannot_mean_anything():
    import pytest
    from stratum_emit.scorers import PreferBareEarth

    with pytest.raises(ValueError, match="hard_floor <= min_soil"):
        PreferBareEarth(min_soil=0.5, hard_floor=0.8)
    with pytest.raises(ValueError, match="sun_weight"):
        PreferBareEarth(sun_weight=-1.0)


# -------------------------------------------------------------------------- BareEarthNadir
class _Obs:
    """The three bands BareEarthNadir reads, in the shape ObsWindow presents them."""

    def __init__(self, soil, sun, view):
        import numpy as np
        self.bands = {"frcov": np.asarray(soil, dtype="float32"),
                      "solar_zenith": np.asarray(sun, dtype="float32"),
                      "view_zenith": np.asarray(view, dtype="float32")}

    def __getitem__(self, alias):
        return self.bands[alias]


def test_bare_earth_nadir_ranks_cover_first_then_illumination_and_nadir():
    """Cover decides; illumination and nadir only break ties among comparable ground. A barer
    pixel seen worse must still win, or the tie-breakers have outranked the thing that matters."""
    import numpy as np
    from stratum_emit.scorers import BareEarthNadir, PreferBareEarth

    s = BareEarthNadir()
    assert s.required_roles == ("frcov", "solar_zenith", "view_zenith")
    out = s.score(_Obs([0.95, 0.70], [60.0, 0.0], [12.0, 0.0]), None)
    assert out[0] > out[1], "cover must outrank both tie-breakers combined"

    # nadir_weight 0 reproduces PreferBareEarth exactly
    obs = _Obs([0.95, 0.70], [60.0, 0.0], [12.0, 0.0])
    z = BareEarthNadir(sun_weight=0.25, nadir_weight=0.0).score(obs, None)
    p = PreferBareEarth(sun_weight=0.25).score(obs, None)
    assert np.allclose(z, p, equal_nan=True)


def test_bare_earth_nadir_prefers_nadir_when_cover_and_sun_are_equal():
    from stratum_emit.scorers import BareEarthNadir

    out = BareEarthNadir().score(_Obs([0.90, 0.90], [0.0, 0.0], [2.0, 11.0]), None)
    assert out[0] > out[1]


def test_bare_earth_nadir_refuses_weights_that_would_outrank_cover():
    import pytest
    from stratum_emit.scorers import BareEarthNadir

    with pytest.raises(ValueError, match="may not do"):
        BareEarthNadir(sun_weight=0.7, nadir_weight=0.4)
    with pytest.raises(ValueError, match="must not be negative"):
        BareEarthNadir(nadir_weight=-0.1)
    with pytest.raises(ValueError, match="hard_floor <= min_soil"):
        BareEarthNadir(min_soil=0.5, hard_floor=0.8)


def test_bare_earth_nadir_nans_unusable_looks_rather_than_ranking_them_low():
    """NaN is "this observation may not occupy this cell" (04 section 4), not a low score."""
    import numpy as np
    from stratum_emit.scorers import BareEarthNadir

    s = BareEarthNadir(hard_floor=0.65, max_solar_zenith=70.0, max_view_zenith=10.0)
    out = s.score(_Obs([0.5, 0.9, 0.9], [0.0, 80.0, 0.0], [0.0, 0.0, 20.0]), None)
    assert np.isnan(out).all()


# ------------------------------------------------------------------------------- Landcover
class _Aux:
    def __init__(self, arr):
        import numpy as np
        self.arr = np.asarray(arr, dtype="uint8")

    def raster(self, alias):
        return self.arr


def test_landcover_excludes_by_name_using_the_default_worldcover_codes():
    import numpy as np
    from stratum_emit.masks import Landcover

    m = Landcover(exclude=("water", "built-up"))
    assert m.space == "map" and m.required_aux == ("landcover",)
    #                    tree water bare built-up grassland
    cover = _Aux([[10, 80, 60, 50, 30]])
    ok = m.valid(None, cover)
    assert ok.tolist() == [[True, False, True, False, True]]


def test_landcover_takes_another_products_codes_as_a_parameter():
    """A different land-cover product means different integers, and nothing can tell one uint8
    raster from another - so the codes are a parameter, not a fork. NLCD 2021: 11 = open water,
    23/24 = developed."""
    from stratum_emit.masks import Landcover

    nlcd = {"water": 11, "developed": 24, "shrub": 52, "barren": 31}
    m = Landcover(alias="nlcd", exclude=("water", "developed"), codes=nlcd)
    assert m.required_aux == ("nlcd",)
    ok = m.valid(None, _Aux([[11, 24, 52, 31, 80]]))
    # 80 is WorldCover's water and means nothing in NLCD - it must NOT be excluded here
    assert ok.tolist() == [[False, False, True, True, True]]


def test_landcover_refuses_a_class_its_codes_do_not_name():
    import pytest
    from stratum_emit.masks import Landcover

    with pytest.raises(ValueError, match="unknown class"):
        Landcover(exclude=("lava",))
    with pytest.raises(ValueError, match="unknown class"):
        Landcover(exclude=("water",), codes={"land": 1})       # water is not in these codes
    with pytest.raises(ValueError, match="codes is empty"):
        Landcover(codes={})
    with pytest.raises(ValueError, match="must be integers"):
        Landcover(exclude=("water",), codes={"water": "80"})


def test_landcover_nodata_is_kept_or_rejected_but_never_a_class():
    import numpy as np
    from stratum_emit.masks import Landcover

    cover = _Aux([[0, 80, 60]])
    assert Landcover(exclude=("water",), on_missing="keep").valid(None, cover).tolist() \
        == [[True, False, True]]
    assert Landcover(exclude=("water",), on_missing="reject").valid(None, cover).tolist() \
        == [[False, False, True]]
