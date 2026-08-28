# Colorado School of Mines tag-up — 28 Aug 2026

**Source:** [`refs/2026-08-28-mines-transcript.md`](../../refs/2026-08-28-mines-transcript.md)

> **Attribution caveat.** The transcript is voice-derived and speaker attribution is unreliable —
> several passages filed under one speaker are clearly another's, and cross-talk is merged. Claims
> below are attributed by *content* rather than by the transcript's headers, and anything
> load-bearing should be confirmed before it is acted on. Timestamps are given so each can be
> checked.

**Participants:** Thomas Monecke, Zaid Al-Attar (Colorado School of Mines); Phil Brodrick, Dana
Chadwick, Jeff Leach (JPL). Mines is working with CMU (Tetracorder on the Pittsburgh
supercomputer) and U. Wisconsin's Macrostrat (harmonized geological maps).

---

## 1. Time-critical: the reprocessing window

Attributed to Phil, [16:12]–[17:42]:

- A self-imposed deadline of **31 Aug** to lock the reflectance changes.
- The reprocessing cycle starts **~first week of September**, for the **entire catalog**.
- It takes on the order of **75 days** to fill in the back catalog → completes **~mid-November 2026**.
- All previous mineral maps are regenerated, **derived from Tetracorder 6**.
- **"The mineral classes shift"** — both from the Tetracorder version and from the reflectance update.
- Known residual issues: spurious calcite detection, partially fixed in v2.

### Why this matters more than anything else in the meeting

For roughly ten weeks the archive is **mixed-vintage**: some granules carry old reflectance and
Tetracorder 5 mineral IDs, some carry new. A run over that window that does not pin a vintage will
silently blend two incompatible products, and the result will look plausible.

**Actions taken:** vintage pinning is now mandatory rather than advisory — see
[02 §3](../specs/02-granule-index.md), [09 §2](../specs/09-run-manifest.md). Added as a top risk.

**Actions outstanding:** confirm with Phil what the version identifier actually looks like in the
delivered metadata (collection version? `build_version`? both?), and whether reprocessed granules
are distinguishable from originals *before* download.

---

## 2. Concrete instrument artifacts to mask

Attributed to Phil, [30:41]–[32:19]. Unusually specific and directly actionable:

| Artifact | Fix |
|---|---|
| Detector edge effects | Clip **5 columns** (conservatively **7**) from the **left and right** of the mineral product |
| Dust on the slit | 2–3 columns down the **very centre**; mostly cleaned up, occasionally visible |
| Top/bottom | **No trimming needed** — "top and bottom are totally fair" |

Rationale given: EMIT is a push-broom, so **every column is a different detector**; scene
boundaries in the along-track direction are an artifact of chopping a continuous strip into
granules for download convenience, and carry no physical meaning.

**Design consequence.** These masks are defined on **cross-track column index — sensor geometry,
not map geometry.** Our `PixelMask` contract was specified against a post-regrid `ObsWindow`,
which cannot express "column ≤ 7". Resolved in [04 §3](../specs/04-cost-functions.md) by
supporting raw-space masks explicitly. Ships as a built-in in `stratum_emit`, not left to user
config.

---

## 3. Fractional cover is a first-class input

Attributed to Phil, [22:35], [27:34]–[28:00]:

- EMIT has an **L2B FRCOV** product: already orthorectified, **on the same grid**, three bands —
  photosynthetic vegetation, non-photosynthetic vegetation, soil.
- Caveat: the current version is **"NPV false-positive happy"** — it reads some soil as NPV.
- V002 used a **65% soil-fraction cutoff**; the rationale was grain-size retrieval, which
  "completely falls apart" below that.
- For mosaicking, the recommendation was to **bump it to ~80%**.

**Design consequence.** `frcov` becomes a declared input role. Because it is already on the target
grid, it needs no regridding — it is the cheapest possible input to a scorer. It also directly
enables the bare-earth scorer in §4.

---

## 4. The "decision tree" idea is a Scorer

Attributed to Zaid, [21:39], describing Thomas's proposal:

> *"…if you have six layers, five of them say vegetation, one of them says [spectral type], then
> just choose the one with spectral type and disregard the vegetation."*

Endorsed in reply ([25:49], "Yeah. We're doing that"). This is exactly a cost function: **prefer
the observation that sees ground.** It should ship as a worked example alongside `MinViewZenith`
— see [04 §4](../specs/04-cost-functions.md).

Related, from the same discussion: a **seasonal** filter is wanted, not merely a date range —
*"it would be very reasonable to take all of the August through November scenes from the Western
US"* ([28:00]). Precedent cited: a team mosaicking the Arabian Shield pre-filtered aggressively on
cloud and constrained time. **Added `month_in` to `GranuleFilter`** — a distinct capability from
`start`/`end`, since the useful window recurs annually.

---

## 5. Framing worth adopting

Attributed to Phil, [23:11]:

> *"At the end of the day, this is a Kalman filter… it's mostly coming up with the appropriate loss
> function that one applies to get the right solution out. What I would love to get us to
> eventually… is a version of the world where we're not destroying all of that information.
> Unfortunately we'll probably have to go in steps, because Tetracorder being a binarized output
> removes some of that information content."*

Three things follow:

1. **Strong validation of deferred reduction.** "Not destroying information" is precisely why the
   design keeps observations rather than collapsing at regrid time, and why the score band is
   persisted.
2. **A lean on an open question.** Phil's earlier question — mode over mineral ID directly, or over
   something continuous first? — now has a direction: reduce over continuous quantities (band
   depth, fit) where possible, because binarization is where information is lost. That said, the
   *delivered* product is still categorical, so this likely means reducing continuously and
   classifying at the end rather than the reverse.
3. **Vocabulary.** Phil thinks in loss functions over a temporal filter. Our `Scorer`/`Reducer`
   split maps onto that cleanly and the specs should use his language where it fits.

Also [9:28], on why the reducer must be pluggable:

> *"Geologically, globally, there are a lot of open-ended hypotheses, especially about temporal
> stability… If you're trying to make a base map, you probably don't care so much. If you're trying
> to look at sand dunes, you care a lot."*

That is the clearest statement yet of *why* temporal aggregation cannot be a fixed algorithm.

---

## 6. Explicitly out of scope

Attributed to Phil, [23:11]: the **"reclaimer"** algorithm — subtracting vegetation components
before mineral mapping, which recovers additional mineral identifications even at ~50% vegetation
— *"is gonna get folded in upstream of the actual mineral mapping… probably to start with, we
won't do that in the mosaicing process."*

Recorded so it is not mistaken for a gap in this design.

---

## 7. Stratum has a second potential consumer

Mines is independently doing the same thing: tiling Tetracorder mineral maps to state- and
country-wide mosaics, with a GUI and a QGIS data stream, moving to AVIRIS-5, and running
Tetracorder on a supercomputer via a CMU wrapper. Their cross-scale ambition (EMIT 60 m →
AVIRIS ~60 cm → UAV 6–10 cm) is a multi-instrument mosaicking problem.

They also independently confirmed two of our findings: **QGIS is the working visualization path**,
and **`earthaccess` is the access mechanism** ([27:11]).

Dana [14:16] framed the relationship as wanting external users to "poke holes" in the product
before it is standardized. Concretely, this means the "semi-generic framework, not an EMIT
program" framing is not speculative — there is a named second user, and multi-instrument support
(which SpectralUtil's airborne readers already partly cover) should stay a design constraint
rather than a someday-feature.

One open question from Zaid worth carrying, [8:36]: why does NASA distribute ten per-mineral
abundance maps rather than a single classified map? That is the D6 collection-layout question
arriving from outside, and it suggests both representations have real users.
