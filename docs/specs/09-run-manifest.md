# 09 — Run Manifest

**Status:** draft · **Depends on:** all component specs · **Depended on by:** [10](10-provenance.md)

One declarative document is the entire interface for driving a run.

---

## 1. Principle

> **Terraform owns the platform. The manifest owns the science.**

Confusing the two is how platforms become unusable by the people they were built for: science
parameters leak into HCL, and then every experiment needs an infrastructure PR. The manifest is
versioned in git, reviewed like code, and hashed into provenance.

| Terraform | Manifest | Neither — just code |
|---|---|---|
| ECR, image build/push | Grid, tiling, blocks | The framework package |
| Batch compute envs, queues | AOI and zones | The plugin package |
| Lambda functions, SFN definition | Time range, epoch, cadence | Contracts, aux accessor |
| S3 buckets + lifecycle | Filters, masks, scorer, reducer, mapper | Tests and fixtures |
| IAM roles, EDL secret | Aux sources | |
| Budgets, alarms | Budget ceilings, output formats | |

---

## 2. Example

```yaml
run_id: cm-zones-2026-annual-r3
description: Critical Minerals annual mineral ID, 4 pilot zones

grid:
  crs: EPSG:4326
  resolution: [0.0003, -0.0003]
  tile_size: 1.0
  origin: [-180, -90]
  block: 512

aoi:
  zones: [bingham-canyon, cuprite-nv, leadville-co, south-central-az]

time:
  start: 2022-08-01
  end:   2026-07-31
  epoch: P1M              # monthly snapshots (the voting population)
  deliver: P1Y            # annual product (the delivery cadence)

inputs:
  index: s3://emit-l3/index/emit-granules.parquet
  roles:
    geometry:       {collection: EMITL1BOBS,  var: obs}
    mineral:
      collection: EMITL2BMIN
      var: group_1_mineral_id
      class_table:                        # where this product keeps its own table
        source: embedded
        path: /mineral_metadata
        key: index
        attributes: [name, record, library, group, url]
    mineral_uncert: {collection: EMITL2BMIN,  var: group_1_band_depth_unc}
    mask:           {collection: EMITL2AMASK, var: mask}
    frcov:          {collection: EMITL2BFRCOV, var: soil}   # already orthorectified
  band_aliases:
    view_zenith:  {role: geometry, band: 5}
    solar_zenith: {role: geometry, band: 4}

aux:
  slope: {uri: "s3://.../slope.tif", kind: continuous,  resampling: bilinear}
  snow:  {uri: "s3://.../snow/{date}.tif", kind: categorical, resampling: nearest,
          temporal: nearest, max_age: P3D}

granule_filter:
  - {build_version: b0107_v02}          # REQUIRED - see 02 section 3
  - {max_cloud_fraction: 0.5, on_missing: fail}
  - {max_solar_zenith: 70}
  - {month_in: [8, 9, 10, 11]}          # recurring seasonal window, not an interval

pixel_mask:
  - {ref: emit.masks:EdgeTrim, columns: 7}     # sensor-space; detector edges
  - {ref: emit.masks:L2AStandard, flags: [cloud, cirrus, water, spacecraft]}
  - {ref: emit.masks:SoilFraction, min_soil: 0.65}

scorer:
  ref: cleanest_nadir
  params: {slope_penalty: 0.5}

reducer:
  ref: mode_through_time
  params: {min_count: 3, ignore: [0, -4], tie_break: highest_score}

outputs:
  bucket: s3://emit-l3-products
  formats: [cog, netcdf]
  stac: true
  render:
    mineral_id:
      mapper: categorical
      classes: "@ref:lumping/cm-v1.yaml"
      on_unmapped: fail
      alpha_from: {band: agreement, domain: [0.3, 0.8], range: [60, 255]}

plugins:
  wheel: s3://emit-l3/plugins/stratum_emit-0.4.2-py3-none-any.whl

budget:
  max_tiles: 64
  max_granules: 12000
  max_vcpu_hours: 400
  on_exceed: require_approval
```

---

## 3. Composition

AMD's layered config is a good idea and we keep it. `amd config print -c base.yml -p
"mosaic<-v6<-filter-fit"` composes a base plus an ordered patch chain, so a variant is a patch
rather than a copied file that drifts.

```
stratum plan -m base.yaml -p zones-pilot -p scorer-v3
```

Implemented as an explicit ordered dict-merge **before** validation, so the merged document is
what gets validated and hashed. Lists replace rather than append — appending is convenient once
and confusing forever.

---

## 4. Budget as a first-class field

Not decoration. The planner computes the real fan-out — tiles × epochs × blocks × granules —
**before any compute is provisioned**, compares it to the ceiling, and on exceedance parks the
execution on a Step Functions task token awaiting human approval.

The difference between one zone and the globe is one line of YAML. A typo in a date range is
otherwise a five-figure mistake, and this pipeline actively invites that mistake.

`stratum plan` emits a **dry-run report** by default — tile count, granule count, estimated bytes
read, projected cache hit rate, estimated vCPU-hours — which should be reviewed in the PR
alongside the manifest diff.

---

## 5. Validation at plan time

Everything below fails in stage 1, loudly, while it is cheap:

- schema validity (Pydantic v2) with precise error locations;
- named scorer/reducer/mapper/mask plugins resolve, and versions are recordable;
- every plugin's `required_roles` present in `inputs.roles`;
- every plugin's `required_aux` declared in `aux` — undeclared reads are refused
  ([05 §5](05-ancillary-data.md));
- aux sources exist and are readable;
- `kind`/`resampling` consistent for each aux source;
- `capability` and `halo` consistent with `block`;
- `epoch` divides `deliver` sensibly;
- every `lumping` entry resolves to **exactly one** row in the contributing granules' embedded
  class tables — matched on attributes, never on positional index — and every class referenced by
  a colour table exists after lumping ([07 §6](07-output-mapping.md));
- the contributing granules' class tables **agree by fingerprint**, or a cross-vintage remap is
  explicitly permitted ([11 §9](11-types.md));
- filter `on_missing` policy explicit;
- **a vintage is pinned**, and the frozen index does not span multiple vintages unless explicitly
  permitted ([02 §3](02-granule-index.md));
- budget present and non-infinite.

---

## 6. Open questions

1. YAML or TOML? YAML matches AMD and reads better for nested structure; TOML has fewer footguns.
   Leaning YAML with a strict loader (no implicit typing).
2. Should `aoi.zones` resolve from a checked-in registry, or take inline geometry? A registry
   makes runs comparable and names stable; inline is more flexible.
3. Do we version the manifest schema itself, so old manifests keep parsing? Almost certainly yes —
   `schema_version` at the top.
4. Should `run_id` be user-supplied or derived from the manifest hash? User-supplied is readable;
   derived is unambiguous. Possibly both: `{user_label}-{hash[:8]}`.
