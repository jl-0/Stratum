# 12 — Data Access

**Status:** draft · **Depends on:** [02](02-granule-index.md), [03](03-regrid-glt.md), [11](11-types.md) ·
**Depended on by:** [06](06-caching.md), [09](09-run-manifest.md)

How bytes get from an archive into a block. Every other spec assumes this path; none of them
described it.

---

## 1. Three responsibilities, deliberately separated

| Concern | Component | Runs | Talks to |
|---|---|---|---|
| **Discovery** — what granules exist | `GranuleSource` | Index build, a periodic job | A live catalogue: CMR, STAC, a filesystem |
| **Access** — a URI becomes readable bytes | `AssetStore` | Every worker, on demand | Object storage, a credential endpoint |
| **Read** — bytes become arrays | `GranuleReader` | Every worker | Nothing external |

> **A run never queries a catalogue.** Discovery happens ahead of time and lands in the index; a
> run reads the frozen index and nothing else ([02 §6](02-granule-index.md)). That is not an
> optimisation. It is what makes a run reproducible, and it is what stops 4,000 concurrent workers
> rate-limiting a public API at the worst possible moment.
>
> **This applies to metadata, not to pixels.** Workers pull granule data straight from S3 when they
> need it. What is settled ahead of time is *which* granules and *where* they live — the URL list —
> not the bytes. See §4.

The seam that matters most is between **access** and **read**. Fusing them is the normal mistake:
a reader that takes a path and opens it can only ever read local files, which is exactly the
position `SpectralUtil` is in ([03 §4](03-regrid-glt.md)). Split, the reader takes an already-open
handle and never learns whether the bytes came from disk, from S3, or from a range request.

### These are hooks, but not science hooks

`GranuleSource` and `GranuleReader` are pluggable, and they are a different **tier** from the five
contracts in [04](04-cost-functions.md):

| Tier | Hooks | You write one to |
|---|---|---|
| **Science** | `GranuleFilter`, `PixelMask`, `Scorer`, `Reducer`, `OutputMapper` | Change the answer |
| **Access** | `GranuleSource`, `GranuleReader` | Add a data source |

A new instrument is a reader. A new archive is a source. Neither changes what a mosaic *means*,
and keeping them in a separate tier is what stops the science hook count drifting upward every
time someone adds a format.

---

## 2. Reading one block

The concrete path, for a work item `(tile=N40W113, epoch=2026-08, block=(3,7))`:

```
 1  win  = block.window                            # (H, W) on the tile grid, halo included
 2  cand = frozen_index.intersecting(block, epoch) # granules whose footprint meets this block
 3
 4  for granule in cand:
 5      glt = assets.open(glt_key_for(granule, tile))   # cached COG from stage 2
 6      g   = glt.read(win)                             # (H, W, 3) int32 — RANGE REQUEST
 7      if not (g[..., 2]).any():
 8          continue                                    # granule does not reach this block
 9
10      sw  = SensorWindow.covering(g[..., 0], g[..., 1])   # bbox of touched sensor pixels
11      src = assets.open(granule.assets["mineral"])        # stage-in, or /vsis3/ stream
12      arr = reader.read(src, var="group_1_mineral_id", window=sw)   # (h, w) SENSOR space
13
14      band, valid = gather(arr, g, sw)                # (H, W) BLOCK space
```

Five things here are load-bearing.

**Step 6 is a windowed read, not a file read.** GLTs are written as COGs whose internal tile size
divides the block size, so a block fetches only the bytes covering its own window.

**Step 10 is the whole point of this spec.** A 512 × 512 block at 0.0003° spans 0.1536°, which is
about 17 km at the equator, or roughly 285 × 285 of a granule's 60 m pixels — fewer in longitude
toward the poles. The granule is 1664 × 1242 ([11 §1](11-types.md)), so the block needs under 4% of
it. Reading the whole scene once per block would be a **~25× read amplification** at the equator and
more at mid-latitudes, paid on every block, of every granule, in every epoch.
`SensorWindow.covering` takes the min and max of the GLT's X and Y bands across the block and reads
that rectangle alone.

**The window carries its origin, not just its shape.** `SensorWindow` is
`(row0, col0, height, width)` against the *full* sensor array. A sensor-space `PixelMask` such as
`EdgeTrim` has to know the absolute crosstrack column to tell whether it is on a detector edge
([04 §3](04-cost-functions.md)); a window that has forgotten where it came from cannot answer
that, and would silently trim the edges of every block instead of the edges of the detector.

**Step 12 names the variable explicitly.** The reader comes from the role's declared `collection`
and the variable from the role's `var` ([02 §5](02-granule-index.md)). Nothing sniffs the filename.
`spec_io.open_netcdf` dispatches on filename substrings, which stops working the moment a file is
staged into a cache-keyed path — a real consequence of stage-in, not a hypothetical one.

**Step 14's gather is the only place the two spaces meet:**

```python
gy, gx, fi = g[..., 1], g[..., 0], g[..., 2]
hit   = fi != 0                                  # 0 is GLT nodata
interp = (gy < 0) | (gx < 0)                     # negative marks an interpolated cell
r = np.abs(gy) - 1 - sw.row0                     # GLT indices are 1-BASED
c = np.abs(gx) - 1 - sw.col0
band  = np.where(hit, arr[r, c], fill)
valid = hit & ~arr_mask[r, c]
```

One-based indices and the negative-means-interpolated convention are inherited deliberately, so
Stratum GLTs interoperate with existing artifacts ([03 §2](03-regrid-glt.md)). Both are easy to get
wrong once and never notice, which is why the gather lives in the framework and not in plugin code.

> Everything above happens **below** `ObsWindow`. A `Scorer` sees the result of step 14 and nothing
> else — no URIs, no sensor windows, no readers, no credentials. That is the same guarantee
> `AuxAccessor` makes for ancillary data ([05 §1](05-ancillary-data.md)), reached from the other
> direction.

### Ortho-native roles skip most of this

Some inputs are already on a map grid. EMIT's L2B FRCOV is orthorectified and ships on the granule's
own ortho grid, so it has no sensor space to window and no `loc` array to KD-tree. A reader declares
which space it produces, and the framework warps ortho-native roles onto the block grid directly —
the cheapest possible input, and the reason FRCOV was worth accepting as a first-class role.

---

## 3. `GranuleReader`

```python
class GranuleReader(Protocol):
    collections: tuple[str, ...]          # index `collection` values this reader claims
    space: Literal["sensor", "ortho"]     # what read() returns

    def open(self, asset: AssetHandle) -> ReaderContext:
        """Cheap. Headers and metadata only — never the pixels."""

    def variables(self, ctx: ReaderContext) -> Mapping[str, VarSpec]:
        """Name -> dtype, shape, fill, units. Used by plan-time validation."""

    def read(self, ctx, var: str, window: SensorWindow | None = None) -> MaskedArray:
        """Fills already resolved to a mask — see 11 section 2."""

    def geolocation(self, ctx) -> LocArray | None:
        """lat/lon/elev in sensor space. The regrid input; None when space == 'ortho'."""

    def class_table(self, ctx, path: str, key: str, attributes) -> ClassTable | None:
        """Pull an embedded table out of the granule being read — see 11 section 9."""
```

Four rules:

1. **The registry is keyed on `collection`**, populated by entry points and overridable in the
   manifest. A reader is selected from what the role *declares*, never from what the file is
   *called*.
2. **The reader is where `-9999` dies.** `read` returns values with fills already converted to a
   mask, per [11 §2](11-types.md). Nothing above a reader ever sees a sentinel — which is the only
   way the `-9999`-versus-`0` distinction survives contact with a scorer.
3. **`open` is cheap and `read` is windowed.** Stage 3 opens the same asset once per block; a
   reader that parses the whole file on `open` turns that into the dominant cost.
4. **Readers live in the plugin package.** `stratum_emit` provides the L2B MIN, L1B OBS, L2A MASK
   and L2B FRCOV readers plus an ENVI reader; `stratum` core ships none.

### The L2B gap is a reader, not a patch

`spec_io.open_netcdf` has readers for EMIT `rdn`, `rfl`, `obs` and `l2a_mask`, and none for the L2B
mineral products — the primary Critical Minerals input ([03 §4](03-regrid-glt.md)). That gap is the
first `GranuleReader` implementation, and writing it against this protocol rather than as a patch to
`spec_io` is what keeps it usable from object storage.

### Two L2B flavours, one role

`tetrapy` writes an xarray Dataset with `group_{N}_{mineral_id, band_depth, band_depth_unc, fit}`
over `downtrack`/`crosstrack`; the DAAC ships `EMITL2BMIN` NetCDF ([02 §5](02-granule-index.md)).
These are two readers claiming two collections and resolving through the same role — which is
precisely the case the registry exists for, and precisely the case filename dispatch cannot express.

---

## 4. `AssetStore`

```python
class AssetStore:
    def open(self, uri: str, *, etag: str | None = None) -> AssetHandle
    def stage(self, uri: str) -> Path                       # this worker, this file, now
    def credentials_for(self, uri: str) -> Credentials
```

Schemes: `file://`, `s3://`, and `https://` behind Earthdata Login.

| Mode | How | When |
|---|---|---|
| **Stage-in** | The worker copies that one asset to its own scratch and gets a path back | Default. Works with any code that takes a filename, including unmodified `SpectralUtil` |
| **Stream** | `/vsis3/` or fsspec; reads become HTTP range requests | Once the reader supports windowed reads. A block touching ~285 × 285 of 1664 × 1242 fetches under 4% of the file |

The sequencing is deliberate and already the plan in [03 §4](03-regrid-glt.md): **stage-in first**,
because it works immediately against code that exists; **streaming second**, because that is where
the cost is. Both satisfy the same `AssetHandle`, so a reader does not change when the mode does.

### When the fetch happens

**Lazily, inside the worker, on first touch.** Nothing is copied before a run starts.

```
plan time      index query        -> URLs, footprints, times, vintages   (METADATA ONLY)
                                     frozen into the run index
worker starts  nothing fetched
first block    assets.open(uri)   -> this worker pulls this one asset, now
later blocks   same worker        -> asset cache hit; no second fetch
```

> **"Stage-in" is per-asset and just-in-time, not a pre-run copy.** It means *this worker downloads
> this one granule to its own scratch at the moment it needs it*. There is no bulk copy of the
> archive into our bucket, no pre-run download pass, and no requirement that anything be resident
> before `stratum run` is invoked. A worker that never touches a granule never fetches it, and a
> granule that no block reaches is never downloaded at all.

The line is **metadata versus pixels**, and the two have opposite economics:

| | Resolved ahead of the run | Fetched in the worker, on demand |
|---|---|---|
| What | Which granules qualify, their asset URLs, footprints, acquisition times, vintages | The pixels |
| Size | Kilobytes | Gigabytes |
| Why there | One query, frozen, so the run is reproducible and CMR sees one caller | Per-block, in thousands of workers, and most of it is never touched |

A CMR query is cheap, returns URLs, and *must* be frozen or the run reproduces nothing — so it
happens once, ahead of time. Pixel data is large, sparse in access, and needed concurrently
everywhere — so it is pulled on demand and never centralised.

> Granule pixels are the one thing that is **never** prefetched. Aux rasters are the exception in
> the other direction: they are small, static, and touched by every block, so they are warped during
> planning ([05 §5](05-ancillary-data.md)).

### The asset cache is not the artifact cache

Easy to conflate, and the two obey different rules:

| | Artifact cache ([06](06-caching.md)) | Asset cache |
|---|---|---|
| Holds | GLTs, observations, snapshots, products | Local copies of upstream granules |
| Keyed by | A hash of the inputs that determine it | URI + ETag |
| Scope | Shared across runs, projects and accounts | One worker or one node |
| Deleting it | Costs recomputation | Costs a download |

Asset *identity* enters artifact cache keys — `asset_roles` in the masked-observation key
([06 §2](06-caching.md)) — but the local copy never does. A staged file is a performance detail with
no bearing on correctness, and it must stay that way: the moment a cache key depends on whether a
file happened to be local, reruns stop being reproducible.

### Credentials

| Path | Mechanism | Lifetime |
|---|---|---|
| LP DAAC over HTTPS | EDL bearer token, or `~/.netrc` | Long |
| LP DAAC direct S3 | Temporary AWS credentials from the DAAC's `s3credentials` endpoint, exchanged with an EDL token | **~1 hour** |
| Your own buckets | Task role or instance profile | Refreshed by the SDK |

> **The one-hour expiry is a design constraint, not an operational footnote.** Any worker living
> longer than an hour must refresh mid-run, and any work item that *cannot finish* in an hour is
> the wrong size. This is an independent argument for blocks over tiles, and for many short tasks
> over few long ones ([08 §1](08-execution.md)). `AssetStore` refreshes on an expiry error and
> retries once, so no reader and no plugin has to know this.

Direct S3 access is **in-region only** — `us-west-2` for LP DAAC. Out of region it fails in a way
that reads like a permissions problem, so `AssetStore` resolves scheme by region and falls back to
HTTPS rather than surfacing the confusing error.

---

## 5. `GranuleSource`, and CMR

```python
class GranuleSource(Protocol):
    name: str
    def search(self, *, collections, bbox, start, end,
               updated_since: datetime | None = None) -> Iterator[GranuleRecord]:
        """Yields catalogue records. Paged internally; may be very long."""

    def assets(self, record: GranuleRecord) -> Mapping[str, str]:
        """Role -> URI. The source owns this policy; the index stores the result."""
```

Shipped implementations: `CMRSource` (the default for EMIT), `StacSource`, `LocalSource`,
`ParquetSource`.

### Why a bridge rather than calling `earthaccess` directly

`earthaccess` handles the genuinely annoying parts — EDL auth, `.netrc`, the S3 credential
exchange, DAAC-specific endpoints — and is already a `SpectralUtil` dependency. That settles open
question 1 in [02 §7](02-granule-index.md): **use it, behind `CMRSource`.**

It is not sufficient on its own. It returns `DataGranule`, a thin wrapper over UMM-G JSON, and four
things have to happen between that and a row in our index:

1. **Map UMM-G onto the index schema.** Not one-to-one, and lossy in places.
2. **Resolve assets to roles.** UMM-G gives `RelatedUrls` typed by access method, not by role.
   Choosing the `s3://` entry over the HTTPS one, and deciding which asset is `mineral` versus
   `mask`, is our policy and belongs in one place.
3. **Apply the null policy.** Cloud fraction lives in `AdditionalAttributes` on some collections and
   is simply absent on others. Absent must arrive as `None`, never `0.0` — the entire filter
   contract in [02 §4](02-granule-index.md) turns on that distinction, and it is exactly the bug
   V002 has.
4. **Isolate global state.** `earthaccess.login()` mutates module-level state. That is wrong in a
   process pool and worse in Lambda, so the bridge holds an explicit session object rather than
   relying on import-time configuration.

| Index column | UMM-G field | Notes |
|---|---|---|
| `granule_id` | `GranuleUR` | Natural key derived from it; the UR is the full producer id |
| `collection` | `CollectionReference.ShortName` | `EMITL2BMIN` etc. |
| `datetime`, `end_datetime` | `TemporalExtent.RangeDateTime.{Beginning,Ending}DateTime` | |
| `geometry` | `SpatialExtent.HorizontalSpatialDomain.Geometry.GPolygons` | EMIT footprints are simple polygons |
| `bbox` | derived from `geometry` | Denormalized for cheap prefilter |
| `cloud_fraction` | `AdditionalAttributes` — **attribute name to confirm** | **`None` when absent** |
| `assets` | `RelatedUrls[]`, `Type` in `GET DATA` / `GET DATA VIA DIRECT ACCESS` | The direct-access entry is the `s3://` URI |
| `build_version` | `PGEVersionClass.PGEVersion` — **candidate, unverified** | Granule-level. See below |
| `product_version` | granule-level; UMM-G location **unverified** | The granule's own `V001` stamp. May require a header scan — see below |
| `collection_version` | `CollectionReference.Version` | The collection's version (`001`). Identical for every granule in the collection, so **never** a vintage predicate |
| `day_night` | `DataGranule.DayNightFlag` | |
| `last_seen` | set by the index build, not from UMM-G | The refresh timestamp — see paging below |

Three rows are marked unverified on purpose. They are the concrete thing to check first against a
real query, and guessing them in a spec would be worse than naming them as open.

> **`collection_version` is not a vintage.** Every granule in `EMITL2BMIN.001` reports
> `CollectionReference.Version = 001`, so the field is constant across the collection and cannot
> tell a reprocessed granule from an original one. If a reprocessing campaign is published as a new
> collection it becomes visible as a *different collection*; if granules are re-delivered in place
> under the same collection — which is the case this design must survive — only the granule-level
> fields move. That is why the index carries all three ([02 §2](02-granule-index.md)) and why the
> vintage predicate is `build_version` / `product_version`, never `collection_version`.

### The vintage question is answered here or nowhere

The open question threaded through [02 §3](02-granule-index.md) and [11 §12](11-types.md) is whether
`software_build_version` and `product_version` are visible **before download**. The granule carries
them as global attributes; whether CMR exposes them is a property of EMIT's UMM-G, and `CMRSource`
is the component that finds out.

Both outcomes are already handled by this design:

- **Exposed** → `build_version` is populated at search time and is a real index predicate. Cheap.
- **Not exposed** → the index build opens each granule's header to record it. Expensive per granule,
  but paid **once, in a periodic job**, not once per run — and the frozen index still hands a run a
  filterable column. The cost lands somewhere it can be afforded.

Either way the run-time contract is identical, which is why it was safe to leave open this long. It
is not safe to leave open past the reprocessing window ([notes](../notes/2026-08-28-mines-tagup.md)),
because that is when a mixed-vintage index becomes silently wrong.

### Paging, and how reprocessing gets noticed

CMR caps `page_size` at 2000 and uses a `CMR-Search-After` header for deep paging; a full-mission
EMIT query is well past the point where offset paging works at all. `search` pages internally and
yields, so an index build streams rather than materialising the catalogue in memory.

`updated_since` does more than make refreshes incremental: **it is how a reprocessing campaign is
detected.** A granule re-delivered with Tetracorder 6 mineral IDs comes back as updated, the index
records a new `last_seen`, and `stratum index verify` reports it ([02 §3](02-granule-index.md)).
Without it, the archive changes underneath a stable query and nothing says so.

### A local run needs no catalogue at all

```yaml
inputs:
  source:
    kind: local
    root: ./granules
    pattern: "EMIT_L2B_MIN_*.nc"
```

`LocalSource` walks the directory and reads each file's header once, producing the same GeoParquet
index the CMR path produces. Slower per granule, trivially reproducible, and it is what makes a
laptop run possible with no network and no Earthdata account.

Nothing downstream can tell which source built the index — which is the test that this seam is in
the right place.

---

## 6. Manifest surface

```yaml
inputs:
  index: ./index/emit-granules.parquet   # what a RUN reads
  source:                                # how that index is BUILT
    kind: cmr                            # cmr | stac | local | parquet
    provider: LPCLOUD
    prefer: direct                       # direct (s3://) | https
  readers:
    EMITL2BMIN: emit.readers:L2BMin      # override the registry; rarely needed
  roles:
    mineral: {collection: EMITL2BMIN, var: group_1_mineral_id}
```

`stratum run` never reads `source`; it touches `index` and nothing else. It sits in the manifest so
that `stratum index build -m manifest.yaml` is driven by the same document a run is, and so
provenance can record where the index came from ([10](10-provenance.md)). A field that one command
uses and another ignores is a mild wart, and the alternative — a second config file that must be
kept in step with the first — is a worse one. See open question 1.

---

## 7. Plan-time validation

All of this fails in stage 1, before compute is provisioned:

- every role's `collection` resolves to exactly one registered reader;
- every role's `var` exists in that reader's `variables()`, checked against one real granule;
- every band alias resolves to a `(role, band)` that exists;
- every asset URI scheme is supported, and credentials for it are obtainable **now**;
- a sample asset opens and reports the expected sensor shape;
- readers agree on space: an ortho-native role is not being asked for a KD-tree regrid.

The rule is the same one the aux accessor follows: a typo fails in the planner, not in four
thousand workers ([05 §5](05-ancillary-data.md)).

---

## 8. Open questions

1. Does `source` belong in the run manifest, or in a separate index-build config? Manifest keeps it
   to one document and one hash; separate keeps `stratum run` from carrying a field it ignores.
2. **Does EMIT's UMM-G expose the build version?** The blocking one — it decides whether the index
   build is a metadata query or a header scan over the whole archive.
3. Should staged assets share a node-level cache across concurrent workers? Obvious win on a SLURM
   node running many blocks of the same tile; needs a lock, and locks on shared filesystems are
   where reliability goes to die.
4. Should `SensorWindow` reads round out to the file's chunk boundaries? A tight bbox can be slower
   than a chunk-aligned over-read on a chunked NetCDF. Measure before deciding.
5. Does multi-instrument support need wavelength awareness in `GranuleReader`, or does band-alias
   mapping cover it? Alias mapping covers EMIT and AVIRIS-3; it probably does not cover resampling
   between differing band centres, which is a real ask if Mines brings AVIRIS-5.
