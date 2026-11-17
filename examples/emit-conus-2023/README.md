# Contender C, CONUS 2023

The mode-gated selector of `../emit-cm-demo-sites/` (contender C: per-group votes, a soft gate,
rank-normalised penalties) on the reprocessed archive, for calendar 2023. The method is explained
step by step in `../emit-cm-demo-sites/manifest-assistant-pick.walkthrough.html`; the scorer block
here is that contender's, unchanged.

| Manifest | Area | Tiles |
|---|---|---|
| `manifest-demo-sites.yaml` | The four demonstration sites | 18 |
| `manifest.yaml` | A box over the conterminous United States, 125 W to 66 W, 24 N to 50 N | 5,184 |

The two differ only in area, the land-cover tiles covering it, the index and the budget. Run the
demonstration sites first: it takes minutes and checks every input the large run reads.

## What differs from the 2025 demonstration runs

| | 2025 runs | Here |
|---|---|---|
| L2B MIN, L1B OBS, FRCOV | V001 | V002 |
| L2A mask | V002 | V003 |
| Mineral classes | 294 (Tetracorder 5) | 312 (Tetracorder 6), read from each granule |
| Cloud | `cloud` + `dilated_cloud` | `spectf_cloud`, with `spectf_buffer: 2` pixels |
| Spacecraft flag | masked | not in the V003 mask |
| Cloud probability (a ranking metric) | mask band 8 | matched by name (band 4 in V003) |

The dilated-cloud flag is a fixed buffer of several kilometres and removed clear ground beside
any cloud, including the pit at the rare-earth demonstration site. The SpecTf flag is the
classifier's own; the buffer trims the margin of the cloud in sensor pixels and should stay at 3
or below. To fall back to the union of the traditional and SpecTf masks, list `cloud` beside
`spectf_cloud`.

## Running

```bash
pixi run stratum validate -m examples/emit-conus-2023/manifest-demo-sites.yaml
make cloud-run-detached MANIFEST=examples/emit-conus-2023/manifest-demo-sites.yaml

make cloud-run-detached MANIFEST=examples/emit-conus-2023/manifest.yaml ARGS=--dry-run
make cloud-run-detached MANIFEST=examples/emit-conus-2023/manifest.yaml
```

The worker image must be rebuilt first: the mask plugin changed, and a worker refuses a plan made
under other code.

## What is not known yet

- **Cost and duration of the CONUS run.** Nothing has been measured. The dry run prints the
  estimate; its budget numbers were sized to admit the box, not from a measurement.
- **Plan time.** The planner stages 157 land-cover tiles (10.3 GB) and warps one per grid tile,
  5,184 of them, before any work is dispatched.
- **Whether the SpecTf flag is too aggressive over bright ground.** Compare the kept fraction
  (`n_valid` against `n_observed`) with the 2025 demonstration runs.
