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
| Lambda functions, SFN definition | Time range, epochs, delivery windows | Contracts, accessors, readers |
| S3 buckets + lifecycle | Filters, masks, scorer, snapshot schema, reducer, mapper | Tests and fixtures |
| IAM roles, EDL secret | Aux sources | |
| Budgets, alarms | Budget ceilings, output formats | |

---

## 2. Example

```yaml
run_id: cm-zones-2026-annual-r3
description: Critical Minerals annual mineral ID, 4 pilot zones

grid:
  crs: EPSG:4326
  resolution: [0.000277778, -0.000277778]   # one arcsecond
  tile_size: 1.0
  origin: [-180, -90]
  block: 720                  # divides the 3600-cell tile exactly
  max_distance: 0.00059       # regrid cutoff; default 1.5 x the grid diagonal - see 03 section 3
  regrid_method: kdtree       # kdtree | warp_embedded - see 03 section 3
  # force_positive_y: true    # only to defeat the guard rail in 01 section 1

aoi:
  zones: [bingham-canyon, cuprite-nv, leadville-co, south-central-az]

time:
  start: 2022-08-01
  end:   2026-07-31
  epoch: P1M              # the voting unit - one snapshot per epoch
  deliver: P1Y            # == {every: P1Y, window: P1Y, align: exact}

# Long form, for a rolling composite delivered more often than the window slides:
#
# time:
#   epoch: P1M
#   deliver: {every: P1M, window: P13M, align: center}

inputs:
  index: s3://emit-l3/index/emit-granules.parquet   # what a RUN reads
  source:                                           # how that index is BUILT - see 12
    kind: cmr
    provider: LPCLOUD
    prefer: direct                                  # direct (s3://) | https
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
    mineral_depth:  {collection: EMITL2BMIN,  var: group_1_band_depth}
    mineral_uncert: {collection: EMITL2BMIN,  asset: MINUNCERT, var: group_1_band_depth_unc}
    mask:           {collection: EMITL2AMASK, var: mask}
    frcov:          {collection: EMITL2BFRCOV, var: soil}   # already orthorectified
  band_aliases:
    view_zenith:  {role: geometry, band: 5}
    solar_zenith: {role: geometry, band: 4}
    # swir_2200:  {role: reflectance, match: {wavelength: 2200}, tolerance: 10}   # by reader-reported attribute

aux:
  slope: {uri: "s3://.../slope.tif", kind: continuous,  resampling: bilinear}
  snow:  {uri: "s3://.../snow/{date}.tif", kind: categorical, resampling: nearest,
          temporal: nearest, max_age: P3D}

# allow_mixed_vintage: true           # top level; requires a documented reason - see section 5

granule_filter:
  # - {build_version: "010635"}         # filterable, not required: the vintage check is the
  #                                     # class-table fingerprint - see 02 section 3
  - {max_cloud_fraction: 0.5, on_missing: fail}
  - {max_solar_zenith: 70}
  - {month_in: [8, 9, 10, 11]}          # recurring seasonal window, not an interval

pixel_mask:
  - {ref: edge_trim, columns: 7}               # sensor-space; detector edges
  - {ref: l2a_standard, flags: [cloud, cirrus, water, spacecraft]}
  - {ref: soil_fraction, min_soil: 0.65}       # refs: entry-point name, or module:Class

scorer:
  ref: cleanest_nadir
  params: {slope_penalty: 0.5}

snapshot:                                 # what resolve writes and how reduce collapses it - see 13
  name: cm-v1
  layers:
    mineral_1:
      kind: categorical
      source: mineral
      classes: "@ref:classes/cm-v1.yaml"  # the product enumeration; also the legend
      aggregate: {method: vote, min_count: 3, ignore: [none], tie_break: highest_score}
    depth_1:
      kind: continuous
      source: mineral_depth
      aggregate: {method: inverse_variance, unc: depth_1_unc, conditional_on: mineral_1, spread: iqr}
    depth_1_unc:
      kind: continuous
      source: mineral_uncert
      aggregate: {method: none}
    view_zenith:
      kind: continuous
      source: view_zenith
      aggregate: {method: none}

# reducer:                                # only for logic the schema vocabulary cannot express
#   ref: my_package:ClassifyLast

outputs:
  bucket: s3://emit-l3-products
  formats: [cog, netcdf]
  stac: true
  render:
    mineral_1:
      mapper: categorical                 # classes come from the layer's enumeration
      on_unmapped: fail
      alpha_from: {band: mineral_1_agreement, domain: [0.3, 0.8], range: [60, 255]}

plugins:
  wheel: s3://emit-l3/plugins/stratum_emit-0.4.2-py3-none-any.whl

budget:
  max_tiles: 64
  max_granules: 12000
  max_vcpu_hours: 400
  on_exceed: require_approval
```

### Three fields that exist only to defeat a default

`grid.max_distance`, `grid.force_positive_y` and top-level `allow_mixed_vintage` are all overrides
of something the framework would otherwise decide or refuse. They are named here rather than left
implicit because `max_distance` enters the GLT cache key ([06 §2](06-caching.md)) — an input that
determines an artifact must be settable — and because the other two are the documented escape
hatches for guard rails in [01 §1](01-grid-tiling.md) and [02 §3](02-granule-index.md).

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

Everything below fails in the plan stage, loudly, while it is cheap:

- schema validity (Pydantic v2) with precise error locations;
- named scorer/reducer/mapper/mask plugins resolve, and versions are recordable;
- every plugin's `required_roles` present in `inputs.roles`;
- every role's `collection` resolves to exactly one registered `GranuleReader`, and its `var`
  exists in that reader's variables ([12 §7](12-data-access.md)); a collection the index holds
  under more than one `collection_version` is pinned with `version:`;
- every asset URI scheme is supported and credentials for it are obtainable now;
- every plugin's `required_aux` declared in `aux` — undeclared reads are refused
  ([05 §5](05-ancillary-data.md));
- aux sources exist and are readable;
- `kind`/`resampling` consistent for each aux source;
- `capability` and `halo` consistent with `block`;
- `deliver.every` and `deliver.window` are whole multiples of `epoch`, and
  `window >= every`; `deliver.align` is `exact` only when `window == every`;
- the snapshot schema resolves: every layer `source` names a role or alias, every categorical
  layer's enumeration resolves to **exactly one** row per member in each contributing granule's
  embedded table — matched on attributes, never on positional index — `conditional_on` and `unc`
  name declared layers, and every class a colour table names exists in the enumeration
  ([13](13-snapshot-schema.md), [07 §6](07-output-mapping.md));
- the contributing granules' class tables **agree by fingerprint**, or a cross-vintage remap is
  explicitly permitted ([11 §9](11-types.md));
- filter `on_missing` policy explicit;
- **the vintage check** is that fingerprint agreement. A disagreement fails the plan unless
  `allow_mixed_vintage: true` is set with a documented reason, and then only if every raw table
  resolves fully into the enumeration ([13 §3](13-snapshot-schema.md)); `build_version` is
  reported, not pinned ([02 §3](02-granule-index.md));
- budget present and non-infinite.

---

## 6. Open questions

1. ~~YAML or TOML?~~ **Resolved:** YAML, with a strict loader and no implicit typing.
2. ~~Should `aoi.zones` resolve from a checked-in registry, or take inline geometry?~~ **Resolved:**
   a checked-in registry, so runs are comparable and names stable.
3. ~~Do we version the manifest schema itself, so old manifests keep parsing?~~ **Resolved:** yes —
   `schema_version` at the top.
4. ~~Should `run_id` be user-supplied or derived from the manifest hash?~~ **Resolved:** both:
   `{label}-{hash[:8]}`.
