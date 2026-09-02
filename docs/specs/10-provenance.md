# 10 — Provenance and Cataloguing

**Status:** draft · **Depends on:** [02](02-granule-index.md), [06](06-caching.md), [09](09-run-manifest.md)

What a run records about itself, so a result can be explained and reproduced.

---

## 1. The claim we have to be able to make

> A run is reproducible from its manifest hash alone.

Same frozen index, same plugin versions, same aux content, same result. Neither existing pipeline
can make that claim: both query a coverage file refreshed on a 24-hour timer, so the same inputs
select different granules on different days and nothing records which.

Reproducibility here is not ceremony. This product will be regenerated across Tetracorder
versions, across reprocessing campaigns, and across years — and someone will need to explain why
two versions of the same tile differ.

---

## 2. The run record

Written by `Finalize`, immutable, kept indefinitely at
`s3://{bucket}/runs/{run_id}/provenance.json`.

```json
{
  "run_id": "cm-zones-2026-annual-r3",
  "manifest_hash": "sha256:...",
  "manifest": { "...": "the fully merged, post-patch document" },
  "schema_version": "1.0",
  "started": "2026-09-02T14:22:11Z",
  "finished": "2026-09-02T15:47:03Z",

  "inputs": {
    "frozen_index": "s3://.../runs/cm-.../index.parquet",
    "frozen_index_hash": "sha256:...",
    "granule_count": 4127,
    "build_versions": {"010632": 1810, "010635": 2317},
    "class_tables": {"EMITL2BMIN": ["sha256:..."]},
    "collections": {"EMITL2BMIN": "001", "EMITL2AMASK": "002"}
  },

  "code": {
    "stratum_version": "0.3.1",
    "stratum_commit": "a1b2c3d",
    "image_digest": "sha256:...",
    "plugin_wheel": "s3://.../stratum_emit-0.4.2-py3-none-any.whl",
    "plugin_wheel_hash": "sha256:...",
    "regrid_algo_version": 3
  },

  "plugins": {
    "scorer":  {"ref": "cleanest_nadir",     "version": "0.4.2", "params": {...}},
    "reducer": {"ref": "schema",             "version": "0.3.1"},
    "mapper":  {"ref": "categorical",        "version": "0.4.2", "params": {...}}
  },

  "schema": {
    "name": "cm-v1",
    "layers_hash": "sha256:...",
    "aggregate_hash": "sha256:...",
    "extends": [{"name": "cm-v0", "layers_hash": "sha256:..."}]
  },

  "aux": [
    {"alias": "slope", "uri": "s3://.../slope.tif", "etag": "...", "resampling": "bilinear"}
  ],

  "filters": [
    {"describe": "cloud_fraction <= 0.5", "removed": 812, "on_missing": "fail"},
    {"describe": "solar_zenith <= 70",    "removed": 143}
  ],

  "execution": {
    "tiles": 4, "epochs": 48, "blocks": 168,
    "cache_hits": {"glt": 3891, "snapshot": 0},
    "vcpu_hours": 41.2,
    "failed_items": []
  }
}
```

Three parts carry the weight. **`inputs`** pins exactly what was read. **`code`** pins exactly what
ran, by digest rather than tag. **`filters`** records what was excluded and why — the counterpart
to the silent-drop problem in [02 §4](02-granule-index.md); a run that quietly discarded 812
granules should say so where someone will see it.

---

## 3. Per-artifact provenance

Every cached artifact carries its `.inputs.json` beside it ([06 §2](06-caching.md)), so any
individual GLT, snapshot or product block can be explained without the run record. Published
products additionally embed `run_id` and `manifest_hash` in their metadata, so an orphaned GeoTIFF
still leads back to its run.

---

## 4. STAC

One item per (tile, delivery period), with assets for the data product, the rendered image, and the
legend.

| Extension | Carries |
|---|---|
| `classification` | Class table — values, names, colours. The legend, in a standard place. |
| `processing` | Software version, image digest, run id |
| `raster` | Per-band data type, nodata, units |
| `proj` | Grid, transform, shape |

Also non-standard but necessary: links back to the frozen index and the provenance record, and the
fingerprint of every input class table the run resolved against.

The class table appearing in the STAC item is what closes the loop on AMD's problem — the meaning
of a colour travels with the product instead of living in a config on a cluster account.

**This catalogue is the delivery boundary.** Stratum writes a STAC collection and its items under
`products/{run_id}` beside the files, and stops. Loading into MMGIS or registering with a DAAC
reads that output; neither is something Stratum does.

---

## 5. The run report

Human-readable, generated alongside the record. Its job is to answer, without anyone opening JSON:

- how many granules were considered, and how many each filter removed;
- cache hit rate by artifact type, and what a rerun would cost;
- which blocks failed, if any, and why;
- estimated cost, and how it compared to the budget;
- a diff against the previous run of the same manifest lineage.

That last one matters most in practice. "This run selected 812 fewer granules than the last one"
is the signal that catches an upstream reprocessing or an index change before it becomes a
mystery in the product.

---

## 6. Open questions

1. ~~Is the STAC item the authoritative catalogue entry, or do we also register with an internal
   catalogue / the DAAC?~~ **Resolved:** out of scope. Stratum writes a STAC catalogue and files
   under `products/{run_id}` and stops there (§4); registering with a DAAC or loading into MMGIS is
   a downstream step that reads that output.
2. ~~Should provenance record *every* cache key used, or just the top-level hashes?~~ **Resolved:**
   top-level hashes. Per-artifact `.inputs.json` files answer "which GLT produced this pixel"
   without the record carrying every key.
3. ~~How do we express lineage across reprocessing — a `derived_from` link to the previous run, or a
   separate lineage graph?~~ **Resolved:** a `derived_from` link to the previous run.
4. ~~Do we need signed/verifiable provenance for a delivered NASA product, or is a JSON record
   sufficient?~~ **Resolved:** a JSON record, until a delivery requirement says otherwise.
