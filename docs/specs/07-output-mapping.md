# 07 — Output Mapping

**Status:** draft · **Depends on:** [00](00-overview.md), [04](04-cost-functions.md) ·
**Depended on by:** [10](10-provenance.md)

How finished bands become the image people look at, without the archive losing the data behind it.

---

## 1. The problem this solves

A mineral class is meaningless on screen until something turns it into colour. That mapping is a
real part of the product: it gets argued over, tuned, and versioned far more often than the
algorithm underneath. It deserves a first-class hook rather than living inside a writer.

But there is a failure mode to avoid, and we have a worked example of it.

### What AMD did

AMD's delivered rasters are `*.colors.tiff` — 4-band RGBA, with the class already rendered. The
colormap exists, in `configs/config.yml`:

```yaml
colors:
  #      R,   G,   B,   A
  -3: [128, 128, 128,   0]   # "fake NaN" -> fully transparent
  -2: [128, 128, 128, 255]   # Tetracorder NaN
   1: [220,   5,  12, 255]   # pyrite
   2: [174, 118, 163, 255]   # goethite
```

It never travels with the product. Three consequences followed:

1. **Nothing downstream can restyle.** MMGIS receives pixels, not classes.
2. **Nothing downstream can recount.** Statistics over a rendered image are meaningless.
3. **The `counts` product was dropped.** The coastal mask is `gdal_rasterize -b 4 -burn 0` —
   band 4, i.e. alpha. It cannot apply to a single-band counts raster, so `gdal.sh` skips it:
   `# Skip counts for now, doesn't work`. A whole product was lost to an RGBA assumption baked
   into the pipeline.

The lesson is not "don't render colour" — the image genuinely is the deliverable people look at.
It is that rendering must not be the *only* thing emitted.

---

## 2. The rule

> **The mapper is a presentation layer over a data product that still ships.**

Stage 5 writes both, from the same block, in the same run:

| Artifact | Content | Consumer |
|---|---|---|
| **Data** | class indices, counts, agreement, score — as declared by the `Reducer` | analysis, reprocessing, statistics, restyling |
| **Image** | RGBA rendering via `OutputMapper` | MMGIS, QGIS, anyone looking at a map |
| **Legend** | class table or ramp stops | both — makes the image interpretable and the data joinable |

The legend is a sidecar *and* an entry in the STAC item. That single requirement is what stops us
recreating a situation where the only record of what a colour means lives in a config file on
someone's cluster account.

---

## 3. Config for the ordinary cases

Most mappings are a lookup table or a ramp and must never require code. These are declared in the
manifest and validated at plan time, so a missing class or a bad domain fails before compute is
provisioned.

### Categorical

```yaml
outputs:
  mineral_id:
    mapper: categorical
    classes: "@ref:lumping/cm-v1.yaml"     # id-keyed, see §6
    colors:
      pyrite:   [220,   5,  12]
      goethite: [174, 118, 163]
      hematite: [209, 187, 215]
    nodata: transparent
    on_unmapped: fail                      # fail | grey | transparent
```

`on_unmapped: fail` is the default deliberately. A class that appears in the data but not the
colormap is a real error — silently rendering it grey is how a mineral goes missing from a map
without anyone noticing.

### Continuous

```yaml
  band_depth:
    mapper: continuous
    ramp: viridis
    domain: [0.0, 0.15]
    clip: true                             # else out-of-domain -> nodata
```

### Confidence-driven alpha

```yaml
  mineral_id:
    alpha_from:
      band: agreement
      domain: [0.3, 0.8]
      range: [60, 255]
```

This generalizes what AMD did by hand — alpha 0 for nodata — into something more useful:
**low-agreement pixels fade rather than asserting a confident colour they have not earned.** For a
mode-through-time product where support varies enormously with revisit, that is close to a
requirement, and it is painful to retrofit once rendering is hard-coded.

Available mappers: `categorical`, `continuous`, `threshold` (classed breaks), `composite`
(three bands → RGB).

---

## 4. A callable for the elaborate cases

The escape hatch is the same plugin mechanism as the other four hooks, so a custom mapper is
versioned, entry-point registered, and hashed into provenance like anything else.

```python
class OutputMapper(Protocol):
    outputs: tuple[ImageSpec, ...]

    def render(self, bands: BandStack, aux: AuxAccessor) -> RGBAArray:
        """(H, W, 4) uint8. Free to consult any finished band, and aux."""

    def legend(self) -> Legend:
        """Class table or ramp stops. Sidecar + STAC. Not optional."""
```

`bands` is every band the `Reducer` declared, so a mapper can reason across them:

```python
class ConfidenceShaded:
    """Class colour, hillshaded by terrain, faded by agreement."""
    outputs = (ImageSpec("mineral_id_shaded", "RGBA"),)

    def render(self, bands, aux):
        rgb = self.table[bands["mineral_id"]]
        rgb = rgb * hillshade(aux.raster("dem"))[..., None]
        alpha = np.interp(bands["agreement"], [0.3, 0.8], [60, 255])
        return np.dstack([rgb, alpha]).astype("uint8")

    def legend(self):
        return Legend.categorical(self.table, note="shaded by terrain")
```

Cases config cannot express: blending where minerals co-occur, hillshading against a DEM pulled
through `aux`, bivariate maps of class against confidence.

---

## 5. Rendering is derived, not authoritative

Because the image is a pure function of the data product plus the mapper, **re-rendering never
re-runs the pipeline.** `stratum render --run <id> --mapper <ref>` reads published data blocks and
writes a new image. Consequences worth designing for:

- Colour debates cost seconds, not a rebuild — which is the same iteration argument that motivates
  the GLT cache, one layer up.
- MMGIS can be given a different rendering of the same archival product without forking it.
- The mapper version is part of the *image's* identity, not the data's, so restyling does not
  invalidate the data cache.

---

## 6. Class tables key on stable IDs

AMD's `hashmap` keys on position — `goethite: [3, 5, 6, 7, 8]`. Positions shift when rows are
added, removed or deduplicated, so a colour or lumping table keyed on `index` **silently reassigns
minerals on a version bump**, with no error. `tetracorder-lite` PR #20 added an `id` column
precisely for this reason.

**But `id` does not exist yet in delivered data.** Measured on a real V001 granule
([11 §9](11-types.md)), `index` is the *only* unique key — `(library, record, group)` collides on
2 of 294 entries, bare `name` on 11. And the index space already differs between vintages: 294
entries in the granule against 312 in `v6.00a6.csv`.

**Requirements, given that reality:**

1. A class table **declares the vintage it was built for** and fails validation against any other.
   V001 tables are vintage-locked by construction; this is not a workaround, it is the honest
   consequence.
2. Tables carry `(library, record, group, name)` beside `index` as **provenance**, so migration
   between vintages is computable and the handful of ambiguous entries surface for manual
   reconciliation instead of being guessed.
3. Prefer reading the table from the granule's own `/mineral_metadata` group over a checked-in
   CSV — it cannot drift from the data it describes.
4. When V002 lands, migrate the primary key to `id` and relax rule 1.

---

## 7. Open questions

1. Should the legend format be a STAC `classification:classes` extension entry, a GDAL colour
   table embedded in the COG, or a standalone JSON? Probably all three — they serve different
   consumers — but the authoritative one should be named.
2. Does MMGIS want RGBA COGs, or single-band-plus-colour-table COGs it styles itself? This
   materially changes what stage 5 emits and is worth asking before we build it.
3. Overviews/pyramids: build them in stage 5, or leave to the MMGIS tiling step? Since tiling
   merges tiles anyway, probably the latter.
