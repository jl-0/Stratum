# Codespaces demo

Opens a codespace with the Stratum environment built and walks a visitor through a real mosaic
run, from a catalogue query to a mineral map served on a forwarded port. No Docker, no image to
publish: the base Ubuntu image plus pixi is the whole environment.

```
[Open in GitHub Codespaces]  ->  environment solved at create time
                                 -> walkthrough: login, index, plan, run, results
```

## What a visitor sees

The codespace opens on [`START-HERE.md`](START-HERE.md), rendered, alongside a terminal running
the walkthrough. Every new shell prints:

```
  Stratum demo
  Build a mineral mosaic of northern Nevada from the EMIT archive, one step at a time.

      .devcontainer/get-started.sh

  Nothing has been started for you -- every step is yours to run.
```

`get-started.sh` shows where you are and offers one step at a time:

```
  [x] 1. Environment        pixi solves GDAL, netCDF4, rasterio and Stratum itself
  [ ] 2. Earthdata login    a free Earthdata account; ~/.netrc or two Codespaces secrets
  [ ] 3. Build the index    one CMR query: 37 granules over the tile for 2026, no pixels yet
  [ ] 4. Plan the run       select, filter, freeze, work lists, a report -- and one download
  [ ] 5. Run the mosaic     regrid, resolve, reduce, publish -- ~5.6 GB, about 6 minutes
  [ ] 6. Open the results   mineral map, agreement and support, on a forwarded port
```

Each step's state comes from what exists on disk, never from a progress file, so the walkthrough
resumes correctly after the codespace has been stopped and started. Stopping a codespace kills
every running process but keeps the filesystem; Stratum's content-addressed cache means a
re-run of step 5 picks up where it stopped.

## Files

| File | Role |
|---|---|
| `devcontainer.json` | Base image, host sizing, `onCreateCommand`, the two optional secrets, port 8080, editor settings |
| `START-HERE.md` | The two-minute intro that opens first |
| `get-started.sh` | The walkthrough |
| `scripts/setup-env.sh` | pixi, the SpectralUtil submodule, `pixi install`; runs at create time and in prebuilds |
| `scripts/install-welcome.sh`, `scripts/welcome.sh` | The banner, added to `~/.bashrc` once |
| `scripts/login.sh` | Earthdata Login via earthaccess; secrets, `~/.netrc`, or an interactive prompt that writes `~/.netrc` |
| `scripts/build-index.sh`, `plan.sh`, `run.sh` | Thin wrappers over `stratum index build`, `stratum plan`, `stratum run` on `examples/emit-cmr-nevada/manifest.yaml` |
| `scripts/show-results.sh`, `tools/make_site.py` | Render the products as PNGs and serve a small page on port 8080 |
| `scripts/watch.sh`, `scripts/reset.sh` | Follow a run started elsewhere; clear runs and products but keep the downloads and cache |
| `../.vscode/tasks.json` | Opens the walkthrough terminal on folder open (`task.allowAutomaticTasks` is set in `devcontainer.json`) |

Everything the demo writes lands under `examples/emit-cmr-nevada/` (`index/`, `out/`), which that
directory's `.gitignore` excludes. Point the scripts at another manifest with
`STRATUM_DEMO_MANIFEST=path/to/manifest.yaml`.

## One-time setup on the fork

Nothing has to be published. Two optional conveniences:

1. **Prebuild.** Settings → Codespaces → Set up prebuild on `main`. `onCreateCommand` solves the
   environment, so a prebuilt codespace opens with step 1 already done instead of spending three
   or four minutes on it.
2. **Secrets for yourself.** [Codespaces user secrets](https://github.com/settings/codespaces)
   `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD`, granted to this repository, make step 2
   automatic for your own codespaces. Visitors without them get the interactive prompt.

## Cost

If you are not already paying for Codespaces, this demo stays well inside the free monthly
allowance of a personal GitHub account (120 core-hours and 15 GB-months of storage at the time
of writing). A full walkthrough is roughly half an hour on the 4-core machine, about 2
core-hours, and the codespace holds around 10 GB while it exists. Storage is billed for as long
as the codespace exists, so delete it when you are done rather than leaving it stopped.

## Sizing

`hostRequirements` asks for 4 cores, 16 GB and 32 GB of storage. Regrid runs one worker per core
and each holds a 3600 × 3600 tile's cell centres plus a KD-tree, about 1 GB; the run downloads
roughly 5.6 GB of granules (37 × 109 MB geometry files and 37 × 43 MB mineral files) and caches
about 100 MB of artifacts. On 2 cores the geometry stage roughly doubles.

## Running it locally

The same scripts work in a plain checkout on Linux or macOS, with `pixi` on the path:

```
.devcontainer/get-started.sh
```

The banner is only installed in a codespace; run the walkthrough directly.
