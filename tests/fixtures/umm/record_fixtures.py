"""Record real UMM-G documents as offline fixtures for tests/test_cmr_source.py (12 section 5).

Run once, live (CMR granule search is anonymous; no Earthdata login is needed):

    STRATUM_LIVE=1 pixi run python tests/fixtures/umm/record_fixtures.py

Writes `<ShortName>.<Version>.json` beside this script: the record's `umm` dict only, minified.
One EMITL2BMIN.001, one EMITL1BRAD.001 and one EMITL2AMASK.002 record from the Nevada pilot
tile (-118, 41) in June 2026, all for the same scene so the granule ids line up.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BBOX = (-118.0, 41.0, -117.0, 42.0)
TEMPORAL = ("2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z")
WANT = [("EMITL2BMIN", "001"), ("EMITL1BRAD", "001"), ("EMITL2AMASK", "002")]


def scene_of(umm: dict) -> str:
    """`20260210T210747_2604114_008` from any EMIT GranuleUR."""
    return "_".join(umm["GranuleUR"].split("_")[-3:])


def main() -> int:
    if os.environ.get("STRATUM_LIVE") != "1":
        print("set STRATUM_LIVE=1 to record fixtures from CMR", file=sys.stderr)
        return 2
    import earthaccess

    found: dict[str, dict] = {}
    scene: str | None = None
    for short_name, version in WANT:
        results = earthaccess.search_data(short_name=short_name, version=version,
                                          bounding_box=BBOX, temporal=TEMPORAL)
        by_scene = {scene_of(r["umm"]): r["umm"] for r in results}
        if scene is None:
            scene = min(by_scene)
        if scene not in by_scene:
            print(f"{short_name}.{version}: no record for scene {scene}; have {sorted(by_scene)[:5]}",
                  file=sys.stderr)
            return 1
        found[f"{short_name}.{version}"] = by_scene[scene]
    for key, umm in found.items():
        out = HERE / f"{key}.json"
        out.write_text(json.dumps(umm, separators=(",", ":"), sort_keys=True) + "\n")
        print(f"wrote {out} ({out.stat().st_size} bytes) {umm['GranuleUR']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
