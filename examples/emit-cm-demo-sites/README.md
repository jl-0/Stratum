# Critical Minerals demonstration sites

Contending mosaic configurations over the same four sites, the same year and the same grid, built
so their products can be flipped between in a map viewer. The point is to judge the configurations
by their maps. None of these is the delivered product.

```bash
pixi run stratum validate -m examples/emit-cm-demo-sites/manifest-baseline.yaml   # no data, no login
pixi run stratum plan     -m examples/emit-cm-demo-sites/manifest-baseline.yaml   # Earthdata login
make cloud-run MANIFEST=examples/emit-cm-demo-sites/manifest-baseline.yaml        # needs the deployment
```

Every manifest here shares `./index`, because their `inputs.source.patterns` are identical. Plan
any one of them first and the others reuse its index.

`tests/test_examples.py` holds these manifests to that: one grid, one index, the same filters,
masks and band names; B equal to `mode_gated_select`'s defaults; C differing from B in exactly
`key`, `gate` and `normalize`. Change a default or a shared block and the suite says which
manifest to follow.

## The sites

| Zone (`../zones.yaml`) | Site | Tiles | 2025 granules (all / ≤ 80 % cloud) | Box status |
|---|---|---|---|---|
| `cuprite-nv` | Cuprite / Goldfield, Nevada | 4 | 30 / 28 | approximate |
| `mountain-pass-ca` | Mountain Pass REE mine, California | 2 (1 shared) | 12 / 10 | unverified |
| `bingham-canyon-amd` | Bingham Canyon mine, Utah: the acid-mine-drainage reference | 1 | 14 / 7 | exact (the AMD project's region) |
| `mojave-l2b-tutorial` | the workshop tutorial area (assumed) | 12 | 60 / 56 | unverified |

18 tiles in total. Granule counts are from CMR, `EMITL2BMIN.001`, queried over each box on
23 Oct 2026. Bingham is thin, so the granule filter is `max_cloud_fraction: 0.8` everywhere,
and the pixel masks remove the cloud itself.

## The grid

This is the **EMIT L3 Gridded Reflectance** lattice ([`emit-sds-l3rfl`](https://github.com/emit-sds/emit-sds-l3rfl),
ATBD §4.2.1): EPSG:4326 at 0.00055°, with cell centres on whole multiples of 0.00055°. In Stratum
terms the edges sit half a cell off those multiples, so `origin: [-0.000275, -0.000275]`.
`tile_size: 0.55` gives exactly 1000 × 1000 cells per tile. All 18 tiles were checked: every cell
centre is an integer multiple of 0.00055°.

The two grids share cell centres, but they do not necessarily fill the same cells. The L3
reflectance gridder fills a cell when it falls inside the scene footprint or within half an input
pixel of it. Stratum fills a cell within `max_distance`, which defaults to 1.5 × the grid
diagonal. This is a parity check still to run; see below.

## The yearly contenders

These are the current set. The five round-one contenders below stay for reference. Every yearly
contender is built the same way:

- **One epoch, the year.** All of a year's looks go into one bin: for geology, three
  back-to-back looks should count three times, not be thinned to one a week.
- **A `stack` scorer decides everything.** `mode_gated_select` sees every look at a cell at once.
  Each group's consensus is its emitted `mode_1` / `mode_2`, delivered as `emit10_1` /
  `emit10_2`. With one epoch, every layer's `aggregate` is the identity, so reduce only carries
  the decision.
- **Votes are on the EMIT-10, not on the 294 native classes.** Each look's class is lumped at the
  gather by `classes/emit10-g1.yaml` and `-g2.yaml`. Two classes in the same EMIT-10 mineral, such
  as two grain sizes of hematite, no longer count as a disagreement.
- **The variants change one thing each from the base.** A difference between two maps then has
  one cause. `tests/test_examples.py` holds the manifests to that.

| Manifest | Changes from the base | Question it answers |
|---|---|---|
| `manifest-yearly-base.yaml` | — (independent per-group modes, soft gate, rank-normalised penalties, non-detects abstain, `min_count: 1`) | the primary |
| `manifest-yearly-min3.yaml` | `min_count: 3`, `below_min_count: none` | must a class be seen in three looks? Below that the group answers `none` |
| `manifest-yearly-nondetect-vote.yaml` | `nondetect: vote` | should a look that saw nothing vote for `none`? It is conservative, and loses intermittently vegetated ground |
| `manifest-yearly-weighted.yaml` | `vote_weight: score`, `vote_power: 8` | should better looks outvote more looks? |
| `manifest-yearly-reference.yaml` | `key: joint`, `gate: hard`, `normalize: fixed` | contender B's joint (G1, G2) selection, on the EMIT-10. A **reference**, not a primary: vegetation degrades group 2 before group 1, so a joint key favours bare ground |

Each yearly contender delivers the same bands:

| Band | What it is |
|---|---|
| `emit10_1`, `emit10_2` | **The product.** Each group's EMIT-10 consensus over the year, or `none` |
| `emit10_look_1`, `emit10_look_2` | The selected look's EMIT-10 class. It differs from `emit10_*` only where no single look carries both groups' modes: about 0.5 % of cells over two Cuprite tiles |
| `mineral_1`, `mineral_2`, `depth_1`, `depth_2` | The selected look's native Tetracorder classes and band depths |
| `acquired_1` | The selected granule. `granules.json` maps the value to its granule id |
| `bare`, `pv` | The selected look's FRCOV, never a median through time |
| `mode_count`, `n_voters`, `n_valid`, `agreement`, `selection_penalty` | The scorer's diagnostics |

**Local trial over two Cuprite tiles** (all of 2025, 1 arcsec grid, no L2A mask role), base against
min3:
- min3 turns 5.7 % of group-1 cells and 11.7 % of group-2 cells to `none`, and changes nothing
  else.
- In group 2, the base's `none` rises from 0.4 % to 12.2 %, taken from every mineral roughly in
  proportion.

### The EMIT-10 grouping

`make_emit10.py` generates both class maps and `colors-emit10.yaml` from the EMIT L2B mineral
grouping matrix. Regenerate them; do not edit the output.

- **The rule.** A constituent belongs to the EMIT-10 mineral with its largest fraction. 25
  constituents are mixtures. An exact tie between minerals is `mixed`. A constituent that reaches
  no EMIT-10 mineral is `other`: non-EMIT-10 minerals, vegetation, man-made materials, and the ten
  rows the matrix marks -1.
- **Two pairs of rows can't be told apart.** Rows 104/108 and 180/181 are identical in every
  attribute the granule's table carries, so they reach `other` through `unmapped`.
- **Class ids are shared by both groups.**

| Group | Classes (constituents) |
|---|---|
| 1 | goethite 12, hematite 14, other 69 |
| 2 | illite-muscovite 18, kaolinite 14, montmorillonite 13, chlorite 12, calcite 8, gypsum 6, dolomite 4, vermiculite 4, mixed 3, other 117 |

The mixtures are why one constituent cannot carry every mineral it contains. Where a fractional
answer is wanted, `spectral_abundance` (`../emit-us-west-2025/`) lets one constituent reach several
classes.

## The round-one contenders

| | Manifest | Epoch → delivery | Selection | Combination |
|---|---|---|---|---|
| **A** | `manifest-baseline.yaml` | P1W → P1Y | `low_vegetation_nadir`: least green, best lit, most nadir look per week | per-group score-weighted `vote` over the weeks |
| **B** | `manifest-mode-gated.yaml` | P1Y → P1Y | `mode_gated_select`, joint (G1, G2) key, hard gate, fixed bounds: the external tiled aggregator's defaults | one scene per cell |
| **C** | `manifest-assistant-pick.yaml` | P1Y → P1Y | B with `key: independent`, `gate: soft`, `normalize: rank` | one scene per cell |
| **D** | *not written* | | the science lead's preferred path | |
| confirmed | `manifest-confirmed.yaml` | P1D → P1Y | A's `low_vegetation_nadir`, but every look is its own epoch | `confirmed_mineral_vote` reducer: per group, votes weighted score × Tetracorder fit; a class needs ≥ 2 agreeing looks; observed and unconfirmed is `none`. No `bare`/`pv`/`npv` delivered |
| score-weighted | `manifest-score-weighted.yaml` | P1Y → P1Y | B with `vote_weight: score`, `vote_power: 8`: a look's vote in the joint mode is (1 − penalty)⁸ | one scene per cell |

Every contender delivers the same band names (`mineral_1`, `mineral_2`, `depth_1`, `depth_2`,
`acquired_1`, `bare`, `pv`, `npv`), so a viewer can flip between them band for band. A also
delivers `acquired_2`, because its two groups can come from different scenes. B and C also deliver
the scorer's own diagnostics (`mode_count`, `n_valid`, `agreement`, `selection_penalty`).

- **`acquired_*` is the selected-granule map**: which scene won each cell, as the winning look's
  start time. `granules.json` beside each product maps a value to its granule id.
- **`bare` / `pv` / `npv` are the FRCOV pass-through**: the cover fractions behind each selection.
  B and C report the selected look's values. A reports the median over the weeks that elected
  group 1's answer.

## Colours

All five share `outputs.colors: "@ref:colors-usgs-1995.yaml"`, so a class is the same colour in
every contender's maps, legends and data COG colour tables. The table is the 1995 USGS AVIRIS
Tetracorder colour groups for Cuprite (`spectroscopy-tetracorder` `cuprite95`, the 1-µm and 2-µm
colour scripts), keyed by EMITL2BMIN.001 class name, with carbonates added in brown. It names 143
of the 294 classes; the rest render grey (`on_unmapped: grey`). It is generated, not hand-edited:
regenerate it rather than editing a colour in place.

## Still to define

None of these is built, and none was invented to fill the gap.

| # | What | Needed for | Owner |
|---|---|---|---|
| 1 | **Contender D**: the science lead's preferred scoring and combination, written as text | D | science lead |
| 2 | **Final boxes** for Cuprite, Mountain Pass and the workshop area. Replace the placeholders in `../zones.yaml`. If the workshop area is not the revised L2B tutorial scene, the 12 Mojave tiles go away | all | science team |
| 3 | **Second-round filtering**: drop scenes that rarely agree with the mode, then select again. This is the aggregator's pass 2 and has no Stratum mechanism yet. B is that workflow without it | B, D | open design |
| 4 | **A filter over the selected-granule map**: smoothing or excising scene-ID patches. This would keep toggling driven by NPV and reject toggling driven by solar zenith. `acquired_*` exists to inspect this, but there is no rule or mechanism to act on it | D | science lead + SDS |
| 5 | **Mineral groupings** beyond the EMIT-10 (e.g. white micas). The yearly set votes on the EMIT-10 (`classes/emit10-*.yaml`), as the science lead asked. Whether `other` should vote at all is open: `ignore: {g1: [other], g2: [other]}` would make the map EMIT-10 only | yearly | science team |
| 6 | **Grid parity with L3 reflectance**: cells filled at scene edges (the `max_distance` vs footprint rule above), and the float behaviour of `origin + n × res` vs `floor(x / p) × p`. Compare one Stratum GLT with one L3RFL file over the same scene | all | SDS |
| 7 | **Cloud probability band**: `cloud_prob` reads V002 L2A MASK band 8, which has not been checked against a delivered file's band names | B, C | SDS |
| 8 | **Where the team views it**. All five runs share a storage root, so `stratum preview`'s run picker already lists them side by side. Still open: which hosted viewer the science team uses (`make viewer-up` exists), and whether flipping by run picker is enough or a same-band swipe is wanted | all | SDS |
| 9 | **A low-agreement negative control**, e.g. a lava flow chosen from the collaborator's mode-match-count maps. Optional | — | collaborator |
