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
> **This applies to metadata, not to pixels.** Workers pull granule data straight from the
> archive when they need it. What is settled ahead of time is *which* granules and *where* they
> live — the URL list — not the bytes. See §4.

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
 5      glt = assets.open(glt_key_for(granule, tile))   # cached COG from regrid
 6      g   = glt.read(win)                             # (H, W, 3) int32 — RANGE REQUEST
 7      if not (g[..., 2]).any():
 8          continue                                    # granule does not reach this block
 9
10      sw  = SensorWindow.covering(g[..., 0], g[..., 1])   # bbox of touched sensor pixels
11      src = assets.open(granule.assets["mineral"])        # stage-in, or /vsis3/ stream
12      arr = reader.read(src, var="group_1_mineral_id", window=sw)   # (h, w) SENSOR space
13      ok  = sensor_masks(arr, sw)                     # EdgeTrim etc. — knows row0/col0
14      band, valid = gather(arr, g, sw, ok)            # (H, W) BLOCK space
15      valid &= map_masks(band, block, aux)            # L2AStandard etc. — on the block
```

Six things here are load-bearing.

**Step 6 is a windowed read, not a file read.** GLTs are written as COGs whose internal tile size
divides the block size, so a block fetches only the bytes covering its own window.

**Step 10 is the whole point of this spec.** A 720 × 720 block at one arcsecond spans 0.2°, which is
about 22 km at the equator, or roughly 370 × 370 of a granule's 60 m pixels — fewer in longitude
toward the poles. The granule is 1664 × 1242 ([11 §1](11-types.md)), so the block needs under 7% of
it. Reading the whole scene once per block would be a **~15× read amplification** at the equator and
more at mid-latitudes, paid on every block, of every granule, in every epoch.
`SensorWindow.covering` takes the min and max of the GLT's X and Y bands across the block and reads
that rectangle alone.

**[observed] The delivered L2B cannot honour that yet.** Each science variable in the DAAC file —
`group_N_mineral_id`, `group_N_band_depth` — is stored as a *single* gzip chunk covering the whole
1664 × 1242 array ([11 §11](11-types.md)). HDF5 decompresses a chunk whole, so a windowed read of
one of those variables decodes the entire scene however small the window: the window bounds
memory, not bytes. `location/lat` and `lon` are contiguous and uncompressed, so their windows are
real, and so are windows into Stratum's own COGs. The 15× is therefore what the read path *can*
save, and the way to collect it is a one-time transcode per asset — prepared assets, §4.

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
interp = (gy < 0) | (gx < 0)                     # negative marks an interpolated cell -> obs.interpolated
r = np.abs(gy) - 1 - sw.row0                     # GLT indices are 1-BASED
c = np.abs(gx) - 1 - sw.col0
band  = np.where(hit, arr[r, c], fill)
valid = hit & ~arr_mask[r, c] & ok[r, c]
```

One-based indices and the negative-means-interpolated convention are inherited deliberately, so
Stratum GLTs interoperate with existing artifacts ([03 §2](03-regrid-glt.md)). Both are easy to get
wrong once and never notice, which is why the gather lives in the framework and not in plugin code.

**Steps 13 and 15 are where masks run — here, not in regrid.** A sensor-space mask sees the sensor
window with its origin, so `EdgeTrim` knows the absolute column; a map-space mask sees the gathered
block and `aux`. Regrid never reads a pixel band or a mask asset ([03 §5](03-regrid-glt.md)). The
`(band, valid)` pair leaving step 15 is the *masked observation* of [06 §2](06-caching.md): cached
when a run asks for it, recomputed from the GLT otherwise.

**Interpolated cells are valid.** A cell whose nearest sensor pixel lay beyond `max_distance` still
carries a real value, reached for rather than measured in place. The gather keeps it and exposes
the flag as `obs.interpolated`, so a scorer can penalise it and a `PixelMask` can exclude it.
Nothing does either by default.

> Everything above happens **below** `ObsWindow`. A `Scorer` sees the result of step 15 and nothing
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

    def glt(self, ctx) -> GLT | EmbeddedGLT | None:
        """The product's own lookup table on its own grid, if it ships one - an EmbeddedGLT,
        11 section 7. Feeds the warp_embedded regrid method - see 03 section 3."""

    def class_table(self, ctx, path: str, key: str, attributes) -> ClassTable | None:
        """Pull an embedded table out of the granule being read — see 11 section 9."""
```

Five rules:

1. **The registry is keyed on `collection`**, populated by entry points and overridable in the
   manifest. A reader is selected from what the role *declares*, never from what the file is
   *called*.
2. **The reader is where `-9999` dies.** `read` returns values with fills already converted to a
   mask, per [11 §2](11-types.md). Nothing above a reader ever sees a sentinel — which is the only
   way the `-9999`-versus-`0` distinction survives contact with a scorer.
3. **`open` is cheap and `read` is windowed.** Resolve opens the same asset once per block; a
   reader that parses the whole file on `open` turns that into the dominant cost.
4. **Readers live in the plugin package.** `stratum_emit` provides the L2B MIN, L1B OBS, L2A MASK
   and L2B FRCOV readers plus an ENVI reader; `stratum` core ships none.
5. **A reader owns band meaning.** It may report per-band attributes — wavelength, FWHM, whatever
   the file carries — in `VarSpec.band_attrs`, and it may subset or resample as it reads. The
   framework asks for a variable and a window and interprets nothing else. Every deployment writes
   at least a score function and a reader; the core never learns what either means.

### As built

`stratum/access/readers.py` and `stratum_emit/readers/` settle these:

- **Instantiation.** A reader class is instantiated with no arguments and cached per
  `(collection, ref)` in the process; readers hold no per-file state (that is the
  `NetCDFContext`), so sharing is safe. There is deliberately **no** check that a reader's
  `collections` tuple names the requested collection: a manifest `readers:` override exists
  precisely to map a foreign collection (the `tetrapy` flavour) onto an existing reader.
- **Fill handling** (rule 2, [11 §2](11-types.md)). `read` masks `_FillValue` and, for float
  variables, non-finite values too; the dtype is never changed and the fill stays under the
  mask. `geolocation()` departs by design: plain `float64` with `NaN` fills, because it is the
  KD-tree input only and the regrid wrapper consumes `NaN`.
- **Class tables** (`class_table`, [11 §9](11-types.md)). Columns come back in the order asked,
  `[key, *attributes]`. A group that does not exist yields `None` — the product ships no table;
  a column that does not exist raises `KeyError` — the manifest named it. Variable-length
  strings become `pa.string`; unsigned ints keep their width.
- **The shipped GLT** (`glt`, [11 §7](11-types.md)). Returned as an `EmbeddedGLT` with band 3
  = `(glt_x != 0) & (glt_y != 0)`, the transform from the file's `geotransform` (GDAL order) and
  the `spatial_ref` WKT verbatim; `None` when any of those is missing.
- **Contexts** (rule 3). One `open` per distinct asset URI per observation — `MIN` and
  `MINUNCERT` are two opens, `group_1` and `group_2` of one file are one — closed in a
  `finally`; a context is never kept across observations or shipped across processes
  (netCDF4 datasets are neither fork-safe nor picklable, which is why the local executor uses a
  spawn pool, [08 §1](08-execution.md)).
- **Coverage.** `L2BMin` is exercised on the reference granule and the trial data. `L1BRad`
  (registered for `EMITL1BRAD` — CMR has no `EMITL1BOBS`; OBS is the second asset of an
  `EMITL1BRAD.001` record) and `L2AMask` are written from SpectralUtil's `open_emit_obs_nc` /
  `open_emit_l2a_mask_nc` layouts (`obs` / `mask` over `(downtrack, crosstrack, bands)`, names
  in `sensor_band_parameters/observation_bands|mask_bands`); OBS is now exercised by the trial
  run, MASK only on synthetic files that copy that layout. `L1BRad` reads whichever asset it is
  handed: on the record's RAD file it reports `radiance` without band names rather than failing
  on the header. `L2BFrcov` opens and reports `variables()` so plan-time validation works, and
  `read` raises `NotImplementedError` naming §2: the ortho-native path is a later slice — and
  the delivered `EMITL2BFRCOV.001` is per-fraction GeoTIFFs (`EMIT_L2B_FRCOVBARE_001_*.tif`,
  `FRCOVPV`, `FRCOVNPV`, their `UNC` twins, `FRCOVQC`), for which no reader is built.
- `LOCAL_PATTERNS` — the EMIT filename globs a `LocalSource` needs — lives in
  `stratum_emit.readers`, not core: filename conventions are domain knowledge (the layering
  rule in [11 §9](11-types.md)).

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
    def open(self, uri: str, *, etag: str | None = None,
             checksum: str | None = None) -> AssetHandle
    def stage(self, uri: str, *, checksum: str | None = None) -> Path   # this worker, this file, now
    def credentials_for(self, uri: str) -> Credentials
    def cache_path(self, uri: str, checksum: str | None = None) -> Path
```

Schemes: `file://`, `s3://`, and `https://` behind Earthdata Login. As built
(`stratum/access/store.py`, 2026-09-02): `file://` and bare paths open in place, stage-in as the
identity; `https://` is staged into the node-local asset cache described below; `http://` is **refused**
(`UntrustedScheme`: a plaintext URL would carry the Earthdata credential in the clear);
`s3://` and any other scheme raise `NotImplementedError` naming this section. A
local asset must **exist at `open()`** — `FileNotFoundError` — so a bad URI fails in plan-time
validation (§7), not in a worker. Paths are made absolute without resolving symlinks, so the
filename the granule id was derived from is preserved. A `checksum` passed for a local file is
accepted and ignored: it is asset identity for cache keys, not something to re-derive per open.

| Mode | How | When |
|---|---|---|
| **Stage-in** | The worker copies that one asset to its own scratch and gets a path back | Default. Works with any code that takes a filename, including unmodified `SpectralUtil` |
| **Stream** | `/vsis3/` or fsspec; reads become HTTP range requests | Once the asset is in a windowable layout. Stratum's COGs and uncompressed variables already are; the delivered L2B science bands are one gzip chunk each and are not (§2), which is what prepared assets are for |

The sequencing is deliberate and already the plan in [03 §4](03-regrid-glt.md): **stage-in first**,
because it works immediately against code that exists; **streaming second**, because that is where
the cost is. Both satisfy the same `AssetHandle`, so a reader does not change when the mode does.

### Prepared assets

A delivered L2B science band is one gzip chunk, so nothing below the file level can make its reads
windowed. The fix is above it. On first touch, the `AssetStore` transcodes the asset once into a
tiled, per-variable COG in the deployment cache, keyed on the asset checksum alone. Every later
open of that asset — any block, any run, any mask, any schema, any grid — is a genuine range
request over it. The cost is one decode-and-encode per asset per deployment and roughly the
asset's size in cache; the science-free key is what makes it shareable by everything. A reader
never learns whether it was handed the original or the prepared form.

Prepared assets are cache artifacts ([06 §2](06-caching.md)), not staged copies: keyed on
`asset_checksum` and `prepare_version`, shared by every run in the deployment, and kept while any
frozen index references them. Stage-in of the original file remains the first step, because it is
what the transcode reads.

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

The one exception is the planner's data-dependent checks (§7). Against a catalogue source the
plan stages **one geometry asset** (the sample-asset check) and **every contributing granule's
class-table asset** (the vintage check reads the embedded table, which lives only in the file).
Those bytes land in the same node-local cache the workers use and are hits for the run, so the
total download volume is unchanged; the first plan is simply where that share of it happens
(measured on `examples/nevada-cmr`: 1.55 GB at plan, 3.5 GB in regrid, 5.0 GB total for 33
granules). Deferring the fingerprint check to the workers was rejected as the "fails in four
thousand workers" anti-pattern.

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
| Keyed by | A hash of the inputs that determine it | The catalogue checksum, else a hash of the URI plus the ETag when one is known (as built, below) |
| Scope | Every run in the deployment | Meant for one node: a directory filled by download-to-temp and atomic rename, so a reader only ever sees a complete file and no lock is needed. The default `{root}/assets` is right for a single machine, which is the only executor built; a cluster deployment points `STRATUM_ASSET_CACHE` at node scratch (`$SLURM_TMPDIR`), because a shared filesystem gains nothing from the rename discipline and pays for every download twice |
| Deleting it | Costs recomputation | Costs a download |

#### The asset cache as built

| Rule | Contract (`store.py`) |
|---|---|
| Location | One rule, `asset_cache_for(root)`: `$STRATUM_ASSET_CACHE` when set, else `{root}/assets` under the storage root (`outputs.bucket`). The planner, the local executor's workers and `stratum exec` all use it, so they stage into and hit one directory. `--asset-cache` on `stratum run` / `stratum exec` sets the environment variable so spawned workers inherit it. `AssetStore()` built with neither raises `AssetCacheUnconfigured` on the first https open — a per-process temp dir was rejected because it would defeat the cache |
| Path | With a catalogue checksum: `{cache}/{hex[:2]}/{hex[:16]}_{filename}`, so two indexes naming the same bytes share one copy and a re-delivered granule lands beside the old one. Without one: `{cache}/uri/{sha256(uri + etag)[:16]}_{filename}` — the ETag, when the caller has one, so a re-delivery under the same URL is a miss and not a permanent stale hit |
| Checksum forms | `sha512:<hex>` (any hashlib algorithm before the colon), or a bare hex digest read by length (128 → sha512, 64 → sha256). Malformed → `ValueError`; a checksum is never silently unverified |
| Miss | Stream through the Earthdata session to `{target}.part-{pid}-{thread}` in 1 MiB chunks, hashing as the bytes arrive; verify; `os.replace` into place. A mismatch raises `ChecksumMismatch` and deletes the partial file. A `200` whose body is `text/html` — Earthdata Login's answer to a request it could not authenticate — is refused, never cached. Two workers racing to one target both rename complete, identical files; the last rename wins harmlessly (question 3). `prefetch(items, workers=8)` stages several assets through a thread pool with one login, which is how the planner fetches the class-table assets (§7) |
| Hit | A complete file already at the path is trusted **without re-hashing**. It was verified when written and the path is keyed on the digest, so a hit is by construction the file that verified; hashing 100 MB per open would cost more than the fetch it saves |
| Handle | `CachedAsset(LocalAsset)`: `uri` stays the remote URI the index named, `path()`/`vsi()` the cached copy, `checksum` the digest verified against |
| Failures | `429`/`502`/`503`/`504` and transport failures (timeouts, resets — a `requests` exception, wrapped as `AssetFetchError(uri, None, <type name>)`) are retried with exponential backoff, `retries=3`, `backoff=1 s`, each attempt to a fresh temp file; then `AssetFetchError`. Any other non-2xx that is not an auth failure raises at once — no retry, no re-login — and so does a 401/403 still refused after exactly one re-login (both statuses in the message). Timeouts `(30 s connect, 300 s read)`, constructor-overridable. No message ever carries a token, password or header |
| Cache location in provenance | `plan.json` `staging.asset_cache` and `provenance.json` `execution.asset_cache` record the directory as information only; it enters **no** cache key |

Every `store.open` in resolve, regrid and the planner passes `checksum=` from
`GranuleRef.checksums` (keyed `{collection}/{asset}`, looked up with `index.refs.role_asset`), so
a remote asset is verified on the way in and a local one is opened as staged.

Asset *identity* enters artifact cache keys — `asset_roles` in the masked-observation key
([06 §2](06-caching.md)) — but the local copy never does. Identity is the catalogue's per-file
checksum where one exists — CMR publishes a SHA-512 for every EMIT file — and the object's ETag
otherwise. A staged file is a performance detail with
no bearing on correctness, and it must stay that way: the moment a cache key depends on whether a
file happened to be local, reruns stop being reproducible.

### Credentials

| Path | Mechanism | Lifetime | As built |
|---|---|---|---|
| LP DAAC over HTTPS | EDL bearer token, or `~/.netrc` | Long | **Yes.** `stratum/access/auth.py`: `earthdata_login()` builds its own `earthaccess.Auth` and tries the `netrc` strategy (`$NETRC` or `~/.netrc`, machine `urs.earthdata.nasa.gov`) then `environment` (`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`); the result is cached per process and never obtained at import or on a local-only run. `EarthdataLoginError` names the strategies tried and the exception *types*, never a message, token or password. The session carries the bearer token on every request, so the store sends it **only to hosts under `TRUSTED_HOSTS`** (`earthdata.nasa.gov`, `earthdatacloud.nasa.gov` — the credential system's own domains); any other https host is fetched with an anonymous `requests.Session()` and no login, and `http://` is refused. `credentials_for(https)` answers `netrc` or `bearer` for a trusted host from an offline look at the netrc file, `none` for any other, and never logs in |
| LP DAAC direct S3 | Temporary AWS credentials from the DAAC's `s3credentials` endpoint, exchanged with an EDL token | **~1 hour** | No. `s3://` is refused; `prefer: direct` is refused at source construction |
| Your own buckets | Task role or instance profile | Refreshed by the SDK | No |

> **The one-hour expiry is a design constraint, not an operational footnote.** Any worker living
> longer than an hour must refresh mid-run, and any work item that *cannot finish* in an hour is
> the wrong size. This is an independent argument for blocks over tiles, and for many short tasks
> over few long ones ([08 §1](08-execution.md)). `AssetStore` refreshes on an expiry error and
> retries once, so no reader and no plugin has to know this.

The retry exists on the HTTPS path: a `401`/`403` re-invokes the store's `auth` callable —
`earthdata_login()` on the first call, `earthdata_login(force=True)` on every later one, so the
retry is a real re-login — rebuilds the session, and repeats the request once. A second refusal
is `AssetFetchError` carrying the URL and both statuses ([08 §5](08-execution.md)). A spawned
worker logs in afresh on its first remote asset; nothing is inherited across the spawn.

Direct S3 access is **in-region only** — `us-west-2` for LP DAAC. Out of region it fails in a way
that reads like a permissions problem, so `AssetStore` will resolve scheme by region and fall
back to HTTPS rather than surfacing the confusing error. Not built: there is no S3 mode to fall
back from yet.

---

## 5. `GranuleSource`, and CMR

```python
class GranuleSource(Protocol):
    name: str
    def search(self, *, collections, bbox, start, end,
               updated_since: datetime | None = None) -> Iterator[GranuleRecord]:
        """Yields catalogue records. Paged internally; may be very long."""

    def assets(self, record: GranuleRecord) -> Mapping[str, str]:
        """Asset name -> URI. The source owns this policy; the index stores the result."""

    def checksums(self, record: GranuleRecord) -> Mapping[str, str]:
        """Asset name -> catalogue checksum; empty when the catalogue has none."""
```

Shipped implementations: `CMRSource` (the default for EMIT) and `LocalSource`, both in
`stratum/access/sources.py`. `StacSource` and `ParquetSource` are specified; the planner refuses
`kind: stac` / `parquet` with `NotImplementedError` naming this section.

### Why a bridge rather than calling `earthaccess` directly

`earthaccess` handles the genuinely annoying parts — EDL auth, `.netrc`, the S3 credential
exchange, DAAC-specific endpoints — and is already a `SpectralUtil` dependency. That settles open
question 1 in [02 §7](02-granule-index.md): **use it, behind `CMRSource`.**

It is not sufficient on its own. It returns `DataGranule`, a thin wrapper over UMM-G JSON, and four
things have to happen between that and a row in our index:

1. **Map UMM-G onto the index schema.** Not one-to-one, and lossy in places.
2. **Resolve assets to names.** UMM-G gives `RelatedUrls` typed by access method, not by role.
   Which file of a record is which asset, and whether the HTTPS or the `s3://` entry is the one a
   worker opens, is our policy and belongs in one place. As built the `patterns` glob names the
   asset and the HTTPS `GET DATA` URL is the one indexed (`prefer: https`, the only mode).
3. **Apply the null policy.** Cloud cover is the top-level `CloudCover` field, present on every
   L2B MIN granule and simply absent on some other collections. Absent must arrive as `None`,
   never `0.0` — the entire filter contract in [02 §4](02-granule-index.md) turns on that
   distinction, and it is exactly the bug V002 has.
4. **Isolate global state.** `earthaccess.login()` mutates module-level state. That is wrong in a
   process pool and worse in Lambda, so `stratum/access/auth.py` builds its own `earthaccess.Auth`
   and hands the store a session from it; `CMRSource` holds the login callable and **never calls
   it** — a CMR granule search is anonymous, so an index build costs no credential lookup.

| Index column | UMM-G field | Notes | As built (`CMRSource`, 2026-09-02) |
|---|---|---|---|
| `granule_id` | `GranuleUR` | Natural key derived from it; the UR is the full producer id | What the **first listed asset's glob** matched on that file's name (`granule_id_from`), the same rule `LocalSource` uses — so an index built either way is interchangeable. The full `GranuleUR` is kept in `attributes.granule_ur` and is the record's `native_id` |
| `collection` | `CollectionReference.ShortName` | `EMITL2BMIN` etc. | The `patterns` key, which is also the `short_name` searched |
| `datetime`, `end_datetime` | `TemporalExtent.RangeDateTime.{Beginning,Ending}DateTime` | | tz-aware UTC; a missing `EndingDateTime` repeats the beginning; no `RangeDateTime` raises `ValueError` naming the UR |
| `geometry` | `SpatialExtent.HorizontalSpatialDomain.Geometry.GPolygons` | EMIT footprints are simple polygons | The first `GPolygon` boundary; else the first `BoundingRectangle` as a box; neither raises `ValueError` naming the UR rather than dropping the record ([02 §4](02-granule-index.md)) |
| `bbox` | derived from `geometry` | Denormalized for cheap prefilter | `geometry.bounds` at row time |
| `cloud_fraction` | `CloudCover`, a top-level UMM-G field | Integer percent. **Verified:** present on every `EMITL2BMIN.001` granule (249,091 of 249,091 on 2026-09-01); **`None` when absent** on a collection that lacks it | Stored as a **fraction in [0, 1]**, `CloudCover / 100`, because the column name, `GranuleRef.cloud_fraction` and the manifest's `max_cloud_fraction` all speak fractions (a percent made `max_cloud_fraction: 0.8` drop every granule). The integer percent is kept verbatim in `attributes.cloud_cover`. Absent stays `None` |
| `assets` | `RelatedUrls[]`, `Type` in `GET DATA` / `GET DATA VIA DIRECT ACCESS` | One record carries **several files** — `EMIT_L2B_MIN_*.nc` and `EMIT_L2B_MINUNCERT_*.nc` — keyed by asset name (`MIN`, `MINUNCERT`) | The HTTPS `GET DATA` URL, per file matching a `patterns` glob (`fnmatchcase` on the file name). The `s3://` direct-access URL is kept in `attributes['s3:<asset>']`. A file matching no glob is **not indexed** — this is how the 1.85 GB RAD file and the browse PNG never reach a run — and a file with no HTTPS link is not an asset even if it has an `s3://` one |
| `checksums` | `DataGranule.ArchiveAndDistributionInformation[].Checksum` | SHA-512 per file. The asset identity that enters cache keys — see §4 | `sha512:<hex>`, lowercase, joined to the URL on the file name; the algorithm name is lowercased with its hyphen removed |
| `build_version` | `AdditionalAttributes.SOFTWARE_BUILD_VERSION` | **Verified.** Granule-level, e.g. `010635`. Not `PGEVersionClass.PGEVersion`, which is the L2B PGE code version (`v1.3.1` across the whole mission) and never moves | Verbatim; `""` when absent |
| `product_version` | *none* | Not in UMM-G. `LocalSource` reads it from the file header; `CMRSource` derives it from the collection version (`001` ↔ `V001`), which is all it has ever tracked | `product_version_of`: `V` + `collection_version` when the version is all digits, otherwise the version string unchanged |
| `attributes` | `AdditionalAttributes`, verbatim | `SOLAR_ZENITH`, `SOLAR_AZIMUTH`, `ORBIT`, `ORBIT_SEGMENT`, `SCENE`, `SOFTWARE_DELIVERY_VERSION`, … — what `max_solar_zenith` filters on | Every `AdditionalAttribute` by `Name`, multi-valued ones comma-joined, plus `granule_ur`, `cloud_cover` and `s3:<asset>` |
| `collection_version` | `CollectionReference.Version` | The collection's version (`001`). Identical for every granule in the collection, so **never** a vintage predicate | Verbatim; the `patterns` pin only when the record lacks it |
| `day_night` | `DataGranule.DayNightFlag` | | Verbatim |
| `last_seen` | set by the index build, not from UMM-G | The refresh timestamp — see paging below | One timestamp per build |

Every row was checked against a live query on 2026-09-01 (`EMITL2BMIN.001`, LPCLOUD, granules
from 2022 through August 2026). The earlier guess that `PGEVersionClass.PGEVersion` carried the
build was wrong, which is why those rows were marked unverified rather than asserted. The
as-built column was exercised on 2026-09-02 against `EMITL2BMIN.001`, `EMITL1BRAD.001` and
`EMITL2AMASK.002` records (`tests/fixtures/umm/`) and by a live build over the Nevada pilot tile
(`examples/nevada-cmr`). `GranuleRecord.raw` is the UMM-G document.

> **`collection_version` is not a vintage.** Every granule in `EMITL2BMIN.001` reports
> `CollectionReference.Version = 001`, so the field is constant across the collection and cannot
> tell a reprocessed granule from an original one. If a reprocessing campaign is published as a new
> collection it becomes visible as a *different collection*; if granules are re-delivered in place
> under the same collection — which is the case this design must survive — only the granule-level
> fields move. That is why the index carries all three ([02 §2](02-granule-index.md)) and why the
> vintage predicate is `build_version` / `product_version`, never `collection_version`.

### The vintage fields are exposed — and they churn

`SOFTWARE_BUILD_VERSION` is a first-class granule attribute in CMR, so `build_version` is populated
at search time and is a real index predicate. No header scan is needed. That closes the question
threaded through [02 §3](02-granule-index.md) and [11 §12](11-types.md).

The same query showed something the design had not assumed. **One collection already spans many
builds.** Sampling `EMITL2BMIN.001` by acquisition year:

| Acquired | `SOFTWARE_BUILD_VERSION` |
|---|---|
| 2022 | `010617`, `010618` |
| 2023 | `010618` |
| 2024 | `010618`, `010620`, `010621`, `010625` |
| 2025 | `010630`, `010632` |
| 2026 | `010635` |

Eight builds in one collection, and none of them a Tetracorder change. A run over 2022–2026 that
pinned a single `build_version` would keep a fraction of the archive and reject the rest, and
"one build = one vintage" would flag every multi-year run as mixed. So `build_version` is
**filterable and reported, never required**. What plan-time validation actually checks is whether
the contributing granules' embedded class tables agree by fingerprint ([11 §9](11-types.md)) — the
test that catches the Tetracorder 6 reprocessing, because a class shift is exactly what it changes.

How that reprocessing will be published also has a precedent now: `EMITL3ASA` exists in CMR as
both a `001` and a `002` collection. If L2B follows, the new vintage arrives as `EMITL2BMIN.002`
beside the old one, `collection_version` distinguishes the rows, and a role pins one with
`version:` ([02 §5](02-granule-index.md)). If granules are instead re-delivered in place, the
fingerprint check catches it and `updated_since` reports it. Both paths are handled; neither is
guessed. The window in which this matters is the reprocessing itself
([notes](../notes/2026-08-28-mines-tagup.md)).

### Paging, and how reprocessing gets noticed

CMR caps `page_size` at 2000 and uses a `CMR-Search-After` header for deep paging; a full-mission
EMIT query is well past the point where offset paging works at all. The paging is inside
`earthaccess`; as built, `CMRSource.search` calls `earthaccess.search_data` per collection, which
**materialises the whole result list** before the source yields from it. That is acceptable
because the build is scoped (below): a tile-and-months query is tens of rows. An unscoped
full-mission build (~250 k L2B records) would need a streaming page iterator over
`DataGranules` / `get_results`, which is not written — question 9.

`updated_since` does more than make refreshes incremental: **it is how a reprocessing campaign is
detected.** A granule re-delivered with Tetracorder 6 mineral IDs comes back as updated, the index
records a new `last_seen`, and `stratum index verify` reports it ([02 §3](02-granule-index.md)).
Without it, the archive changes underneath a stable query and nothing says so. As built it maps
to CMR's `revision_date` as an open-ended range `(updated_since, None)`, which `earthaccess`
exposes by that name; no client-side filtering is needed. `stratum index build --since` is a
**refresh, not a rebuild**: the rows the query returns replace their earlier versions in the
existing file, keyed on (`collection`, `collection_version`, `granule_id`) — `merge_index` —
and every unrevised row is kept with its old `last_seen`; it refuses when no index exists, or
when the manifest is wider than the index's recorded scope. `stratum index verify` is not built.

### CMR as built

`CMRSource(patterns, *, provider="LPCLOUD", prefer="https", login=earthdata_login)`:

| Rule | Contract (`sources.py`, `plan/run.py`) |
|---|---|
| Query | `query_parameters(collection, bbox, start, end, updated_since)` → `earthaccess.search_data(short_name, provider, version?, bounding_box, temporal=(start, end), revision_date=(updated_since, None))`. `version` only when `patterns` pins one, so an unpinned search records whatever CMR returns |
| Scope | An index built from a catalogue source is **scoped to the manifest**: `index_scope` passes `m.aoi_bbox()` and `[time.start, time.end)`. The query is cheap and the answer is frozen per run, so there is nothing to gain by indexing the mission. A local source indexes its whole directory. The scope is **recorded in the file** (parquet metadata `stratum:scope`: source kind, provider, collections, bbox, start, end, built_at — `index_scope_record`) and `plan_run` refuses (`PlanError`, `check_index_scope`) an index that does not cover the manifest — a wider AOI or time window, a collection the roles read that was not built, another source kind, or no recorded scope at all — so widening `time.end` after a build can never plan silently against the narrower index ([02 §6](02-granule-index.md)) |
| Time window | CMR matches temporal **overlap**; `search` re-applies the acquisition-start window `[start, end)` so `LocalSource` and `CMRSource` answer identically for the same window |
| Footprint | CMR intersects the query box with the footprint **polygon**; `LocalSource` has only the header bounding box. A CMR index can therefore hold fewer granules than a local one over the same tile (12 versus 14 for June 2026 on the Nevada tile — the two extras touch the tile with their box and not their footprint, and contribute no pixels). The ids of the shared granules are identical |
| Role pins | The planner folds a role's `version:` into the collection's long-form `patterns` entry (`pin_role_versions`) so the search filters on it. Two roles pinning different versions of one collection, or a pin disagreeing with a long-form `version`, is a `PlanError` ([02 §5](02-granule-index.md)) |
| Skipped records | A record whose **first** asset glob matches no file has no primary file and no id: it is skipped, counted in `CMRSource.skipped[collection]` and logged at `WARNING`; `build_index_from_manifest` logs the totals |
| Login | Never, during search. `login` is held for the authenticated paths and is not invoked by an index build |
| `prefer` | `https` only. `direct` raises `NotImplementedError` naming §4 at construction |
| Granule-id interchangeability | Holds when a manifest uses the **same** globs for local and CMR: `EMIT_L2B_MIN_*.nc` yields `001_2026…`, `EMIT_L2B_MIN_001_*.nc` yields `2026…`; both work, an index built with one is not interchangeable with one built with the other. `LocalSource` reads its header from the first asset in sorted name order while `CMRSource` takes the id from the first *listed* asset; these agree whenever every glob of a collection yields the same id, which is true of every EMIT pattern |

### A local run needs no catalogue at all

```yaml
inputs:
  index_location: ./index       # a directory; `stratum plan` writes granules.parquet here when absent
  source:
    kind: local
    root: ../../trial-data      # relative to the manifest
    patterns:                   # {collection: {asset: glob}}; the id is what `*` matched
      EMITL2BMIN:
        MIN: "EMIT_L2B_MIN_001_*.nc"
        MINUNCERT: "EMIT_L2B_MINUNCERT_001_*.nc"
      EMITL1BRAD:                 # the CMR short name; OBS is its second asset, RAD is not listed
        OBS: "EMIT_L1B_OBS_001_*.nc"
```

`LocalSource` walks the directory and reads each file's header once, producing the same GeoParquet
index the CMR path produces. Slower per granule, trivially reproducible, and it is what makes a
laptop run possible with no network and no Earthdata account.

Nothing downstream can tell which source built the index — which is the test that this seam is in
the right place.

The contracts `stratum/access/sources.py` fixes:

| Rule | Contract |
|---|---|
| Pattern shapes | `patterns: {collection: {asset: glob}}` is **the one shape for every source**. Locally the glob runs on disk under `root`; against CMR each glob's basename is an `fnmatchcase` over the record's file names (`file_matches`), a file matching no glob is not indexed, and a collection with no entry is not searched. The long form `{collection: {version: "001", assets: {asset: glob}}}` pins a `collection_version` explicitly, detected by an `assets` key whose value is a mapping — so an asset literally named `assets` needs the long form. The one-glob `pattern:` is honoured only when every role reads **one** collection and agrees on its asset; the planner turns it into `{collection: {asset: glob}}` and refuses otherwise. `SourceSpec` allows `patterns` for `kind: local` and `cmr` (`root`/`pattern` stay local-only); a `cmr` source without `patterns` still validates and is refused by the planner with "declares no patterns" — so `patterns` is required for `cmr` at build time |
| Granule id | The filename minus the glob's literal prefix and suffix — what the wildcards matched (`granule_id_from`): `EMIT_L2B_MIN_*.nc` over `EMIT_L2B_MIN_001_20260825T151308_2623710_050.nc` gives `001_20260825T151308_2623710_050`, so the `MIN` and `MINUNCERT` files of one granule land in one record as two assets. Both sources call it on the collection's first asset |
| Header source | The header is read from the **first asset in sorted name order** (`MIN` before `MINUNCERT`) — global attributes only, never a variable. A file lacking `time_coverage_start/end` or the four `*most_*` attributes raises `ValueError` naming the file rather than being dropped silently, which is the failure mode [02 §4](02-granule-index.md) warns about |
| Attributes | Every scalar global attribute verbatim; arrays (`geotransform`) are not index attributes; numpy scalars become Python scalars. Absent `build_version`/`product_version` are stored as `""`, and the built-in equality filters treat `""` as missing so `on_missing` governs it |
| `collection_version` | The pinned value → the id's leading all-digit token (`001`) → the header's `product_version` without its `V` → `""` |
| `cloud_fraction` | `None`, always: absent locally, which is meaningful. A `max_cloud_fraction` filter with the default `on_missing: fail` therefore refuses every granule of a local source. From CMR it is a real fraction, so the same filter is real there |
| `search` | `bbox`, `start`, `end`, `collections` are all optional (a local index build passes none). `updated_since` filters on file **modification time**, the nearest local analogue of a catalogue revision date; naive datetimes are UTC |
| `checksums` | Empty. There is no catalogue checksum for a local file, and a size/mtime stand-in would leak locality into cache keys, which §4 forbids. `build_index` calls `source.checksums(record)` only when the source defines it |

`stratum index build -m manifest.yaml` is the explicit build and the only path that takes
`--since` / `--collection`; it prints the source kind and provider, rows and granules, a line per
collection (rows, collection version, rows with checksums, first..last acquisition) and the time
span, and needs no Earthdata login. `stratum plan` builds `inputs.index_location` implicitly
when the file is absent from **any** `inputs.source` the planner can construct — `local`
indexing every collection in `patterns` over the whole directory, `cmr` scoped to the manifest's
AOI and time — so a laptop run and an archive run are each one command; `stac` and `parquet`
refuse. `LocalSource` understands NetCDF headers with ACDD names only; an ENVI or other local
source would need a header-reader hook (question 8).

---

## 6. Manifest surface

```yaml
inputs:
  index_location: ./index/               # what a RUN reads - a directory; the file name inside is fixed
  source:                                # how that index is BUILT
    kind: cmr                            # cmr | stac | local | parquet  (stac, parquet: not built)
    provider: LPCLOUD                    # default LPCLOUD
    prefer: https                        # https is the only built mode; direct (s3://) is refused
    patterns:                            # required for cmr at build time; the same shape as local
      EMITL2BMIN:
        MIN: "EMIT_L2B_MIN_001_*.nc"
        MINUNCERT: "EMIT_L2B_MINUNCERT_001_*.nc"
      EMITL1BRAD:
        OBS: "EMIT_L1B_OBS_001_*.nc"     # RAD is not listed, so it is never indexed or fetched
  readers:
    EMITL2BMIN: emit.readers:L2BMin      # override the registry; rarely needed
  roles:
    geometry: {collection: EMITL1BRAD, asset: OBS, var: obs}
    mineral:  {collection: EMITL2BMIN, var: group_1_mineral_id}
```

`stratum run` reads `source` only to build a missing index; a run with an index touches
`index_location` and nothing else. It sits in the manifest so that `stratum index build -m
manifest.yaml` is driven by the same document a run is, and so provenance can record where the
index came from ([10](10-provenance.md)). A field that one command uses and another mostly
ignores is a mild wart, and the alternative — a second config file that must be kept in step
with the first — is a worse one. See open question 1. `index_location` and `source` are each
optional, and at least one is required; a run whose index file is absent gets it built from any
source the planner can construct — `local` or `cmr` — and is refused for `stac` / `parquet`
(§5). `SourceSpec` (`manifest/models.py`): `root` and `pattern` are `local`-only, `patterns` is
`local` or `cmr`, `provider` and `prefer` are read for `cmr`; a `cmr` source without `patterns`
validates and fails at plan time.

---

## 7. Plan-time validation

All of this fails in the plan stage, before compute is provisioned:

- every role's `collection` resolves to exactly one registered reader;
- every role's `var` exists in that reader's `variables()`, checked against one real granule;
- every band alias resolves to a `(role, band)` that exists;
- every asset URI scheme is supported, and credentials for it are obtainable **now**;
- a sample asset opens and reports the expected sensor shape;
- readers agree on space: an ortho-native role is not being asked for a KD-tree regrid.

The rule is the same one the aux accessor follows: a typo fails in the planner, not in four
thousand workers ([05 §5](05-ancillary-data.md)).

As built (`stratum/plan/run.py`, `inspect_granules`): the first three checks and the sensor-space
one run against every contributing granule's header — `variables()` and the embedded class table,
never a pixel; `band:` is validated against the band count and `match:` resolved to an index.
Every plan-time open goes through the `AssetStore` with the asset's catalogue checksum, so
against a catalogue source the planner **downloads** what it inspects: one geometry asset for
the sample-asset check and every contributing granule's class-table asset for the vintage check,
staged into the node-local asset cache and hits for the run (§4). The class-table assets are
staged together through `AssetStore.prefetch` (eight threads, one login) rather than one serial
download per granule, and the count is bounded by `budget.max_granules`: the budget gate runs
**before** these checks, so a plan that is going to be refused downloads nothing
([09 §4](09-run-manifest.md)). A metadata-only read of the embedded table (a range-request
HDF5 reader, or a fingerprint recorded in the index at build time) would remove the download
altogether and is not built. That first https open is where
a missing or bad Earthdata credential fails — `EarthdataLoginError` or `AssetFetchError` in the
plan, not in a worker — which is the credential check in practice; `credentials_for` exists and
is not yet called by the planner (question 10). The report gains an "Assets staged at plan time"
section and `plan.json` a `staging: {asset_cache, assets, bytes, uris}` block. `s3://` is
refused as a later slice. One more check belongs here and exists: a granule lacking an asset for **any role that will be read**
— the schema's sources, the scorer's and every mask's `required_roles` through their aliases, and
the geolocation role — is dropped by the planner and counted as a filter row ("provides an asset
for every role read …"), so the report and provenance say so. Resolve raises on such a granule,
which a planned run therefore never reaches. On the trial data this is what removes MIN-only
granules when the scorer needs OBS geometry.

---

## 8. Open questions

1. ~~Does `source` belong in the run manifest, or in a separate index-build config?~~ **Resolved:**
   the manifest. One document, one hash; `stratum run` ignoring a field is the cheaper wart.
2. ~~Does EMIT's UMM-G expose the build version?~~ **Resolved: yes**, as
   `AdditionalAttributes.SOFTWARE_BUILD_VERSION` — verified against CMR on 2026-09-01. See §5.
3. ~~Should staged assets share a node-level cache across concurrent workers?~~ **Resolved:** yes,
   node-local. Workers on a node share a directory filled by download-to-temp and atomic rename; a
   reader only ever sees a complete file, a duplicated download costs bandwidth and never
   correctness, and no lock exists to go wrong. A shared filesystem is never used for this (§4).
   Built 2026-09-02 as `{root}/assets` / `$STRATUM_ASSET_CACHE`.
4. ~~Should `SensorWindow` reads round out to the file's chunk boundaries?~~ **Resolved:** by
   observation, there is no boundary to round to. The delivered L2B stores each science variable as
   one gzip chunk, so a windowed read decodes the whole variable whatever the window (§2). What
   remains is whether to transcode — question 6.
5. ~~Does multi-instrument support need wavelength awareness in `GranuleReader`, or does band-alias
   mapping cover it?~~ **Resolved:** no. The core has no concept of a wavelength or of any band's
   meaning. A reader reports whatever per-band attributes its file carries in `VarSpec.band_attrs`,
   and a band alias may select a band by matching one of them instead of by index (§3, [11
   §5](11-types.md)) — the same attribute matching the class tables use — so a scorer that reads
   `obs["swir_2200"]` moves between instruments through configuration. Resampling between differing
   band centres, if anyone wants it, is a reader's business, done as it reads.
6. ~~**Transcode assets on first touch** into tiled per-variable COGs in the deployment cache
   (§4)?~~ **Resolved:** yes. Every asset is prepared once on first touch and cached by its checksum
   (§4). Not built yet; the trial confirmed the cost it removes — every observation read decodes
   the whole 1664 × 1242 variable, cheap at int16 but paid per (granule, block).
7. ~~CMR has no `EMITL1BOBS` short name: `earthaccess.search_data(short_name="EMITL1BOBS")` returns
   nothing, and the OBS file is the second asset of an `EMITL1BRAD.001` record beside
   `EMIT_L1B_RAD_*.nc`. The manifests name `EMITL1BOBS` because `LocalSource` lets them.~~
   **Resolved:** the collection is `EMITL1BRAD` everywhere — manifests, the reader registry
   (`stratum_emit.readers:L1BRad`) and the index — and the geometry role is
   `{collection: EMITL1BRAD, asset: OBS, var: obs}`, the same one-record-several-files shape as
   `MIN`/`MINUNCERT`. `patterns` names only the OBS asset, which is what keeps the 1.85 GB RAD
   file out of the index and off the network (§5). Verified against CMR 2026-09-02, with
   `EMITL2AMASK` at version `002` and `EMITL2BFRCOV.001` as per-fraction GeoTIFFs
   ([09 §6](09-run-manifest.md), question 6).
8. `LocalSource` reads NetCDF headers with ACDD attribute names and nothing else. A local ENVI
   source — the `tetrapy` / cluster `.img` case in §3 — needs a header-reader hook that has not
   been designed.
9. `CMRSource.search` materialises each collection's result list (`earthaccess.search_data`)
   rather than streaming pages. Fine for the AOI/time-scoped builds the planner issues; an
   unscoped full-mission build wants a page iterator over `DataGranules` / `get_results`.
10. Plan-time credential checking is implicit: the first https open at plan time logs in and
    fails loudly, but `AssetStore.credentials_for` is not called by the planner, so a manifest
    whose plan opens no remote asset is not checked. Wire it, or accept the implicit form.
11. ~~Filter reports count index **rows**, not granules, and a collection without `CloudCover`
    indexed beside MIN would trip `on_missing: fail` on its own rows.~~ **Resolved:**
    `apply_filters` evaluates the chain on one row per granule (`stratum.filters.granule_level`:
    each scalar takes the first present value across the granule's rows, `attributes` the
    union), reports count granules, and a dropped granule takes all of its rows with it.
    `on_missing: fail` is a `PlanError` in the plan, not a traceback.
12. `CMRSource` records each file's `SizeInBytes` as `attributes['size:<asset>']`, but the
    planner has no byte threshold yet: a `patterns` glob that names the 1.85 GB RAD file would
    be staged. A glob matching two files of one record is refused at index time.
12. Direct S3 (`prefer: direct`, `s3://` in the store) is refused. It needs the in-region check,
    the `s3credentials` exchange and the one-hour refresh; nothing local waits on it.
