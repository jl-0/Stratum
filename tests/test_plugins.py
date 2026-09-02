from __future__ import annotations

from stratum.plugins import resolve
from stratum_emit.scorers import MinViewZenith


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
