# Topaz matched filter: a continuous output

Every other example here delivers classes. This one delivers a number per cell: how much a cell
looks like topaz against its own surroundings, from EMIT L2A reflectance. It shows a continuous
scorer output, and it is the first example whose scorer needs a halo across tile edges.

```bash
pixi run stratum validate -m examples/emit-topaz-mf/manifest.yaml     # no data, no login
pixi run stratum plan     -m examples/emit-topaz-mf/manifest.yaml     # Earthdata login
make cloud-run MANIFEST=examples/emit-topaz-mf/manifest.yaml          # needs the deployment
```

The full year is 57 granules and about 109 GB of reflectance (each `EMIT_L2A_RFL_001` file is
about 1.9 GB). Run it in the cloud. A July-August subset is 17 granules and 32 GB.

`manifest-barstow.yaml` runs the same scorer over the whole L3-grid tile holding the GEMx topaz
find near Barstow, California, with daily epochs so `mf_max` (and `band_depth_208_max`) is a max
over every look of the year: did any look ever see it. Read it against `mf`, the median. A steady
signal is high in both; a spike in one look only in the max. In a local August 2025 trial it found
a coherent hit at 34.707 N, 117.133 W (`mf` 0.26, the median of three looks, with a sharp 2086 nm
band in every look); see SE-Documentation/Tasks/Topaz-Matched-Filter/.

## The sites

| Zone (`../zones.yaml`) | What | Tiles |
|---|---|---|
| `topaz-mountain-ut` | Topaz Mountain, Thomas Range, Utah: the classic topaz-in-rhyolite locality | 2, split at 39.6 N |
| `cuprite-core` | Cuprite, Nevada: the negative control. Rich in kaolinite and alunite (which absorb at 2.17 um, where topaz has its second feature), no reported topaz | 2, split at 117.15 W |

Both boxes straddle a tile edge on purpose, so the run shows whether the output steps there.

## What `matched_filter` does

Per look, over each block plus a 200-cell halo:

1. **Bands and continuum.** Keep the good bands from 2.00 to 2.30 um. Divide each spectrum, and the
   target, by the straight line through the means of two shoulders (2.00-2.03 and 2.26-2.30 um).
   That removes brightness and slope, so what is left is mostly the shape of the absorptions.
   This is how Tetracorder fixes a feature's continuum, and it vectorises, where a per-pixel
   convex hull does not.
2. **Local background.** One mean and covariance per window of a lattice anchored to the grid
   (a centre every 100 cells, about 6 km; each window uses the valid cells within 100 cells of its
   centre). The covariance is shrunk 5 % toward its diagonal, and the window's most topaz-like 1 %
   of cells are left out of its own background.
3. **Blend.** Each cell is scored under the four windows covering it, and the four scores are
   blended with tent weights that sum to one. The background therefore moves smoothly from window
   to window, with no edge where a window ends. A window with too few valid cells (cloud, the swath
   edge) is dropped and its neighbours carry the cell.

The target is the USGS splib06 spectrum of topaz from Mt. Antero (record 5040 of the EMIT-convolved
`s06emitc` library). It is the entry Tetracorder 6.00a uses for topaz, and EMIT L2B index 239. It
is packaged with the plugin (`stratum_emit/targets/topaz-mt-antero-5.csv`). `make_target.py` here
regenerates it from the specpr library.

## Reading `mf`

`mf` is about 0 over background and about 1 for a cell that is entirely topaz. In between it is
**topaz's share of the cell's continuum brightness, not of its area.** Dividing by the continuum
turns linear mixing into `a·Ct / (a·Ct + (1-a)·Cb)` for an areal fraction `a` of a target with
continuum `Ct` in a background with continuum `Cb`. Topaz is bright (its continuum is about 0.89),
so 10 % topaz in a 0.3 rhyolite reads about 0.25. Treat `mf` as a ranking and a relative
abundance, not an areal fraction.

`band_depth_208` is the 2.08 um feature's depth against its own local continuum (Tetracorder's
topaz f1a shoulders), with no statistics at all. Where `mf` is high and `band_depth_208` is not,
the filter has found something that is unusual near 2.17 um but lacks topaz's 2.08 um band, such as
kaolinite or alunite. Look at the two together.

`bg_n` is how many background cells stood behind each estimate. It is also the scorer's score,
so within a month the look with the best-supported background wins. That choice never uses `mf`
itself, because then the month's value would be a maximum over looks. The year's months are
combined by median.

## MNF

`k: null` keeps every component. The matched filter already weights each direction by how much the
background varies along it, so a noisy direction adds little. Truncating to the high-SNR
components can also drop the low-variance directions in which a rare target stands out. Setting
`k` to an integer projects onto the top-`k` MNF components (noise estimated from spectral second
differences) before filtering. Comparing `k: null`, 30 and 15 shows whether truncation removes
noise or signal here.

## Tile edges

The halo is 200 cells, so a block near a tile edge needs the next tile's ground. Stratum pads every
per-tile artifact (GLT, aux and ortho warps) by the scorer halo (spec 01 section 4), so it reads
the neighbouring ground, and the blended background continues across the edge as it does across a
block edge. Without that padding the halo across a tile edge is empty, and each tile estimates the
windows on its edge from its own side only.
