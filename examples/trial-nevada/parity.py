"""Parity check: Stratum's `MinViewZenith` product against SpectralUtil's fused GLT
(first-slice plan section 1; heritage: the V002 `build_obs_nc` mosaic stands in for a V002 cell).

Runs from the repo root, after `stratum run -m examples/trial-nevada/manifest-parity.yaml`:

    pixi run python examples/trial-nevada/parity.py [--n-cores 4]

1. Builds the V002-style fused GLT over exactly the trial grid with
   `spectral_util.mosaic.mosaic.build_obs_nc` (criteria: OBS band 2, to-sensor zenith, `min`),
   skipping the build when the file already exists.
2. Applies that GLT to `group_1_mineral_id` and to OBS band 2 with Stratum's gather rule
   (`stratum.resolve.gather`): 1-based indices, negative = interpolated, band 3 = file index.
3. Compares cell-wise with the parity run's `mineral_1` COG and its snapshot `view_zenith`.

`spec_io.load_data` has no L2B reader, so `apply_glt` cannot do step 2; the gather is done here.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

import netCDF4 as nc
import numpy as np
import rasterio

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "out"
RES = 1.0 / 3600.0
TILE = (-118, 41)                       # lon [-118, -117), lat [41, 42)
VIEW_ZENITH_BAND = 2                     # OBS band order as the file stores it (0-based)


def latest_run(label: str) -> Path:
    runs = sorted((OUT / "runs").glob(f"{label}-*"), key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit(f"no run under {OUT / 'runs'} for {label}; run stratum first")
    return runs[-1]


def plan_granules(run_dir: Path) -> tuple[list[Path], list[Path], float]:
    """The run's granules in id order (== acquisition order, the candidate order resolve
    uses) as (OBS files, MIN files) from the frozen plan's asset URIs."""
    doc = json.loads((run_dir / "plan.json").read_text())
    granules = doc["context"]["granules"]
    obs, mins = [], []
    for gid in sorted(granules):
        assets = granules[gid]["assets"]
        obs.append(Path(assets["EMITL1BRAD/OBS"].removeprefix("file://")))
        mins.append(Path(assets["EMITL2BMIN/MIN"].removeprefix("file://")))
    return obs, mins, float(doc["context"]["max_distance"])


def build_fused_glt(obs_files: list[Path], out_file: Path, max_distance: float,
                    n_cores: int) -> float:
    """`build_obs_nc` over the tile grid. Its `target_extent_ul_lr` are cell CENTRES (the
    geotransform origin is ul - res/2), so the tile corner is offset by half a cell here;
    checked against Stratum's transform after the build."""
    from spectral_util.mosaic.mosaic import build_obs_nc

    h = RES / 2
    ul_lr = (TILE[0] + h, TILE[1] + 1 - h, TILE[0] + 1 + h, TILE[1] - h)
    file_list = out_file.with_suffix(".files.txt")
    file_list.write_text("".join(f"{p}\n" for p in obs_files))
    t0 = time.perf_counter()
    build_obs_nc.callback(str(out_file), str(file_list), None, RES, -RES, ul_lr, "4326",
                          VIEW_ZENITH_BAND, "min", n_cores, max_distance, None, "WARN")
    return time.perf_counter() - t0


def read_fused(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        assert src.shape == (3600, 3600), src.shape
        t = src.transform
        assert abs(t.c - TILE[0]) < 1e-9 and abs(t.f - (TILE[1] + 1)) < 1e-9, t
        assert abs(t.a - RES) < 1e-12 and abs(t.e + RES) < 1e-12, t
        return np.moveaxis(src.read(), 0, -1).astype(np.int32)


def gather_fused(glt: np.ndarray, min_files: list[Path], obs_files: list[Path]
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Stratum's gather rule per file index: r = |gy| - 1, c = |gx| - 1 (1-based GLT)."""
    gx, gy, fi = glt[..., 0], glt[..., 1], glt[..., 2]
    hit = fi != 0
    mineral = np.full(hit.shape, -9999, dtype=np.int16)
    zenith = np.full(hit.shape, np.nan, dtype=np.float32)
    for k, (mf, of) in enumerate(zip(min_files, obs_files, strict=True), start=1):
        sel = fi == k
        if not sel.any():
            continue
        r = np.abs(gy[sel]) - 1
        c = np.abs(gx[sel]) - 1
        with nc.Dataset(mf) as ds:
            v = ds.variables["group_1_mineral_id"]
            v.set_auto_maskandscale(False)
            mineral[sel] = np.asarray(v[:])[r, c]
        with nc.Dataset(of) as ds:
            v = ds.variables["obs"]
            v.set_auto_maskandscale(False)
            zenith[sel] = np.asarray(v[:, :, VIEW_ZENITH_BAND])[r, c]
    interpolated = hit & ((gx < 0) | (gy < 0))
    return hit, mineral, zenith, interpolated


def stitch_snapshot_layer(run_dir: Path, layer: str) -> np.ndarray:
    """A resolve-stage layer over the tile, from the snapshot directories the run's resolve
    results point at."""
    out = np.full((3600, 3600), np.nan, dtype=np.float32)
    for line in (run_dir / "work" / "resolve.results.jsonl").read_text().splitlines():
        rec = json.loads(line)
        with rasterio.open(Path(rec["key"]) / f"{layer}.tif") as src:
            t = src.transform
            r0 = round((TILE[1] + 1 - t.f) / RES)
            c0 = round((t.c - TILE[0]) / RES)
            out[r0:r0 + src.height, c0:c0 + src.width] = src.read(1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-cores", type=int, default=4)
    ap.add_argument("--run-label", default="trial-nevada-parity")
    args = ap.parse_args()

    run_dir = latest_run(args.run_label)
    obs_files, min_files, max_distance = plan_granules(run_dir)
    missing = [p for p in obs_files + min_files if not p.is_file()]
    if missing:
        sys.exit(f"missing granules: {missing[:3]}")
    print(f"run {run_dir.name}: {len(obs_files)} granules, max_distance {max_distance:.6g}")

    fused = OUT / "parity" / "fused_glt.tif"
    fused.parent.mkdir(parents=True, exist_ok=True)
    if fused.is_file():
        print(f"fused GLT exists: {fused}")
    else:
        secs = build_fused_glt(obs_files, fused, max_distance, args.n_cores)
        print(f"build_obs_nc: {secs:.1f} s for {len(obs_files)} files ({args.n_cores} cores)")

    glt = read_fused(fused)
    t0 = time.perf_counter()
    hit, f_min, f_zen, f_interp = gather_fused(glt, min_files, obs_files)
    print(f"gather through fused GLT: {time.perf_counter() - t0:.1f} s")

    products = sorted(glob.glob(str(OUT / "products" / run_dir.name / "*" / "*" / "mineral_1.tif")))
    assert len(products) == 1, products
    with rasterio.open(products[0]) as src:
        s_min = src.read(1)
        s_nodata = int(src.nodata)
    s_valid = s_min != s_nodata
    s_zen = stitch_snapshot_layer(run_dir, "view_zenith")

    f_valid = hit & (f_min != -9999)
    both = s_valid & f_valid
    n = float(hit.size)
    print()
    print("=== footprint")
    print(f"fused GLT hit          {hit.mean():.4f}   (interpolated {f_interp.mean():.4f})")
    print(f"fused valid (MIN!=fill){f_valid.mean():.4f}")
    print(f"stratum valid          {s_valid.mean():.4f}")
    print(f"both valid             {both.mean():.4f}   ({int(both.sum())} cells)")
    print(f"fused only             {(f_valid & ~s_valid).sum() / n:.5f}  "
          f"({int((f_valid & ~s_valid).sum())} cells)")
    print(f"stratum only           {(s_valid & ~f_valid).sum() / n:.5f}  "
          f"({int((s_valid & ~f_valid).sum())} cells)")
    print(f"fused hit but MIN fill {(hit & ~f_valid).sum() / n:.5f}  ({int((hit & ~f_valid).sum())})")

    agree = s_min.astype(np.int64) == f_min.astype(np.int64)
    print()
    print("=== mineral_1 (classes: source, product id == raw id)")
    print(f"agreement where both valid   {agree[both].mean():.6f}")
    dis = both & ~agree
    print(f"disagreeing cells            {int(dis.sum())}")
    print(f"  of which fused interpolated {int((dis & f_interp).sum())}")
    dz = np.abs(s_zen - f_zen)
    same_look = both & np.isfinite(dz) & (dz < 1e-4)
    print(f"  of which same view zenith   {int((dis & same_look).sum())}  (same look, different "
          "sensor pixel or class)")
    print(f"  of which zenith differs     {int((dis & ~same_look).sum())}")
    if dis.any():
        pairs, counts = np.unique(np.stack([s_min[dis], f_min[dis]], 1), axis=0, return_counts=True)
        top = np.argsort(-counts)[:8]
        print("  top (stratum, fused) pairs:", [(int(a), int(b), int(c))
                                               for (a, b), c in zip(pairs[top], counts[top])])

    print()
    print("=== view zenith (stratum snapshot layer vs OBS band 2 through the fused GLT)")
    ok = both & np.isfinite(s_zen) & np.isfinite(f_zen)
    print(f"identical (<1e-4 deg)        {(dz[ok] < 1e-4).mean():.6f}")
    print(f"stratum lower                {(s_zen[ok] < f_zen[ok] - 1e-4).mean():.6f}")
    print(f"fused lower                  {(f_zen[ok] < s_zen[ok] - 1e-4).mean():.6f}")
    print(f"max |diff| {np.nanmax(dz[ok]):.4f} deg; mean |diff| {np.nanmean(dz[ok]):.6f}")
    # the winning file: fused file index vs stratum's, via zenith equality per candidate
    print()
    print("=== fused-only cells by reason")
    fo = f_valid & ~s_valid
    print(f"fused-only & interpolated    {int((fo & f_interp).sum())}")
    print(f"fused-only & not interpolated{int((fo & ~f_interp).sum())}")
    so = s_valid & ~f_valid
    print(f"stratum-only & fused hit     {int((so & hit).sum())}  (fused chose a pixel whose MIN is fill)")
    print(f"stratum-only & no fused hit  {int((so & ~hit).sum())}")


if __name__ == "__main__":
    main()
