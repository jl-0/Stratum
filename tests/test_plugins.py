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
