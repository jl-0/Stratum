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

Publish writes both, from the same block, in the same run:

| Artifact | Content | Consumer |
|---|---|---|
| **Data** | class indices, counts, agreement, score — as the schema's aggregations deliver them, or a `Reducer` plugin declares. A categorical band embeds a colour table derived from its enumeration | analysis, reprocessing, statistics, restyling |
| **Image** | RGBA rendering via `OutputMapper` | MMGIS, QGIS, anyone looking at a map |
| **Legend** | class table or ramp stops | both — makes the image interpretable and the data joinable |

The legend is authoritative in the STAC item's `classification` extension; the COG's colour table
and the JSON sidecar are derived from it. That single requirement is what stops us
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
    layer: mineral_1                       # the categorical layer; its enumeration is the legend
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
  depth_1:
    mapper: continuous
    ramp: viridis
    domain: [0.0, 0.15]
    clip: true                             # else out-of-domain -> nodata
```

### Confidence-driven alpha

```yaml
  mineral_1:
    alpha_from:
      band: mineral_1_agreement
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

`bands` is every band the reduction delivered — derived from the schema, or declared by a
`Reducer` plugin — so a mapper can reason across them:

```python
class ConfidenceShaded:
    """Class colour, hillshaded by terrain, faded by agreement."""
    outputs = (ImageSpec("mineral_1_shaded", "RGBA"),)

    def render(self, bands, aux):
        rgb = self.table[bands["mineral_1"]]
        rgb = rgb * hillshade(aux.raster("dem"))[..., None]
        alpha = np.interp(bands["mineral_1_agreement"], [0.3, 0.8], [60, 255])
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

## 6. Lumping resolves through the product's own class table

AMD's `hashmap` keys on raw position — `goethite: [3, 5, 6, 7, 8]`. Positions shift when the
reference matrix gains, loses or deduplicates rows, so a table keyed on them **silently reassigns
minerals on a version bump**, with no error.

The fix is not a better external table. It is to stop maintaining an external table at all:
**every delivered granule embeds its own class table** ([11 §9](11-types.md)), so the authority
travels with the data and cannot drift from the pixels it describes.

Lumping is therefore expressed against **semantic attributes**, resolved per granule at plan time,
and applied at the gather in resolve so snapshots hold product classes
([13 §3](13-snapshot-schema.md)). The file is the categorical layer's `classes`:

```yaml
# classes/cm-v1.yaml
match_on: [library, record, group]     # attributes from the embedded table
classes:
  - {id: 1, name: goethite, members: [{library: sprlb06, record: 882,  group: 1},
                                      {library: splib06, record: 5736, group: 1}]}
  - {id: 3, name: pyrite,   members: [{library: splib06, record: 2568, group: 1}]}
```

Nothing here names a positional index, so the same lumping file survives a vintage change. What
changes between vintages is which integers those attributes resolve *to*, and that resolution is
recomputed per run from the granules actually being read.

**Requirements:**

1. Lumping matches on declared attributes, never on the raw pixel value.
2. Resolution happens at plan time against the granules' embedded tables; an entry matching zero
   or multiple rows is a **plan-time error**, not a runtime surprise. Measured on the delivered
   file, `(library, record, group)` uniquely resolves 292 of 294 entries, so the ambiguous handful
   surface as errors to be reconciled explicitly rather than guessed.
3. The mosaic publishes **its own** class table — the enumeration, with its explicit ids — which is
   the legend requirement in §2 arrived at from the other direction. Ids are never renumbered
   ([13 §5](13-snapshot-schema.md)).

None of this is Tetracorder-specific: the framework matches attributes it was told to match, and
`stratum_emit` supplies only the knowledge that `/mineral_metadata` is where EMIT keeps its table.

---

## 7. Open questions

1. ~~Should the legend format be a STAC `classification:classes` extension entry, a GDAL colour
   table embedded in the COG, or a standalone JSON?~~ **Resolved:** STAC `classification:classes` is
   authoritative. The GDAL colour table in the COG and the JSON sidecar are derived from it at
   publish.
2. ~~Does MMGIS want RGBA COGs, or single-band-plus-colour-table COGs it styles itself?~~
   **Resolved:** both. The data COG for a categorical layer embeds a colour table derived from the
   enumeration, so a viewer that styles single-band rasters can use it directly, and the RGBA
   rendering ships beside it.
3. ~~Overviews/pyramids: build them in publish, or leave to the MMGIS tiling step?~~ **Resolved:**
   leave them to the tiling step, which merges tiles anyway.
