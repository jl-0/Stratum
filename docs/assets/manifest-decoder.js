/* Stratum documentation — the interactive manifest decoder.
 *
 * Loaded only by guide/manifests.html. Renders a REAL manifest at its true line
 * numbers, colours every key it can explain, and puts the explanation in a panel
 * beside it. Modelled on the Tetracorder expert-file primer, for the same reason
 * that page works: a reader learns a config format by clicking the thing in front
 * of them, not by reading prose about it somewhere else.
 *
 * Three rules it keeps:
 *
 *   verbatim   The listings are the checked-in example manifests, byte for byte.
 *              `scripts/sync-manifest-decoder.py` writes them in and
 *              `tests/test_docs_manifests.py` fails when they drift, so the page
 *              cannot quietly describe a manifest nobody runs.
 *   themed     Colours come from CSS custom properties, so the decoder follows
 *              light/dark with the rest of the site. No literal colour here.
 *   degrades   With no JavaScript the listing is still a readable YAML block and
 *              every entry in DICT is reachable from the reference page.
 *
 * No build step, no ES modules: see CLAUDE.md.
 */
(function () {
  'use strict';

  var root = document.getElementById('decoder');
  var data = document.getElementById('manifest-data');
  if (!root || !data) { return; }

  var MANIFESTS = JSON.parse(data.textContent);

  /* ---- categories: label, and the CSS class that colours the token ---- */
  var CAT = {
    structure: ['Structure', 'c-structure'],
    geometry:  ['Geometry', 'c-geometry'],
    time:      ['Time', 'c-time'],
    binding:   ['Input binding', 'c-binding'],
    aux:       ['Ancillary', 'c-aux'],
    plugin:    ['Plugin', 'c-plugin'],
    schema:    ['Snapshot schema', 'c-schema'],
    output:    ['Output', 'c-output'],
    budget:    ['Budget', 'c-budget']
  };

  /* ---- the dictionary -------------------------------------------------
   * key            "block.field", or a bare field as the fallback
   * cat            one of CAT
   * short          one line, shown under the token name
   * body           HTML: what it decides
   * ex             a literal example, shown as code
   * gotcha         the thing that catches people
   * invalidates    what re-running costs after you change it. This is the
   *                manifest-specific payload - the equivalent of the primer's
   *                "what EMIT ignores" note - and is why the panel exists.
   */
  var DICT = {
    /* ---------------- top level ---------------- */
    'run_id': { cat: 'structure', short: 'The run label. Not the run id.',
      body: '<p>A stable name for what this manifest computes. The actual <b>run id</b> is this plus the date and a hash of the whole manifest, so two edits of the same file never share an output directory.</p>',
      ex: 'run_id: emit-cmr-cuprite-bare-earth',
      gotcha: 'Changing anything at all in the manifest changes the run id, because it hashes the merged document. That is the point: a directory of products can always be traced to exactly one manifest.' },
    'description': { cat: 'structure', short: 'Free text, carried into provenance.',
      body: '<p>Never interpreted. It travels into <code>provenance.json</code> so someone reading a product a year later knows what it was for.</p>' },
    'schema_version': { cat: 'structure', short: 'The manifest schema this file targets.',
      body: '<p>Defaulted, and you will not normally write it.</p>' },
    'grid': { cat: 'geometry', short: 'The lattice everything lands on.',
      body: '<p>Every tile, block and output cell derives from this block. It is also the most expensive thing in the file to change.</p>',
      invalidates: 'crs, resolution, origin or tile_size rebuild EVERYTHING from geometry up - every lookup table, every warp, every snapshot. block_size is the exception.' },
    'aoi': { cat: 'geometry', short: 'Which tiles. Exactly one of three forms.',
      body: '<p><code>bbox</code> and the tiles follow; <code>tiles</code> named outright; or <code>zones</code> looked up in a shared registry.</p><p>Tiles are <b>derived</b> from a box: every tile whose nominal bounds intersect it. A box landing exactly on tile edges pulls in no extra row.</p>',
      gotcha: 'Prefer zones for anything delivered. A name in a registry means two runs a year apart cover the same ground, and the report says cuprite-nv rather than four floats nobody can check.',
      invalidates: 'Nothing. Lookup tables are keyed on granule and grid, snapshots on their own block window - neither mentions the AOI, so widening it rebuilds only the new tiles.' },
    'time': { cat: 'time', short: 'The window, the voting unit, and the product period.',
      body: '<p>Four fields that answer two different questions: how finely observations are bucketed, and how those buckets collapse.</p>' },
    'inputs': { cat: 'binding', short: 'Where data comes from, and what it is called.',
      body: '<p>The chain: <code>source.patterns</code> decides what the index may contain, <code>roles</code> decides what this run reads, <code>band_aliases</code> names one band of a role.</p>' },
    'aux': { cat: 'aux', short: 'Data that is not an observation.',
      body: '<p>A DEM, a landcover map, claim polygons. Declared by URI, addressed by <b>alias</b>, staged and warped onto every tile at plan time.</p>',
      gotcha: 'The test for whether something belongs here: would it be a different file for a different granule? If yes it is a role, not aux.' },
    'granule_filter': { cat: 'plugin', short: 'Drop whole granules, before a byte is read.',
      body: '<p>Runs at plan time against the frozen index. Rejecting early is free; the same predicate at pixel level costs a download, a regrid and a scoring pass.</p>',
      ex: 'granule_filter:\n  - {max_cloud_fraction: 0.8, on_missing: fail}',
      gotcha: 'on_missing defaults to fail, deliberately. A granule whose record lacks the key is an error while it is cheap to fix, not a silent drop - which is what the pipeline this replaces did.' },
    'pixel_mask': { cat: 'plugin', short: 'Drop cells. Boolean, never a preference.',
      body: '<p>Sensor-space masks run before the gather, map-space ones after. They compose by conjunction: a cell is eligible only if every mask admits it.</p>',
      gotcha: '"This pixel is unusable" and "this pixel is merely worse" are different statements. The second belongs in the scorer - a mask that expresses preference is a bug you cannot see in the output.',
      invalidates: 'Every snapshot. Mask identity is in the masked-observation key, so lookup tables survive and resolve re-runs.' },
    'scorer': { cat: 'plugin', short: 'Which observation wins a cell.',
      body: '<p>Required. Takes <code>ref</code> (an entry-point name or <code>module:Class</code>) and <code>params</code>. Higher score wins; NaN means this observation may not occupy the cell at all.</p>',
      ex: 'scorer:\n  ref: prefer_bare_earth\n  params: {hard_floor: 0.65}',
      gotcha: 'The scorer decides WHO wins, never WHAT is recorded - that is the snapshot schema. Which is why any scorer works with any schema.',
      invalidates: 'Every snapshot, and the products under them. Lookup tables and warps survive, so this is the cheap experiment: seconds, not minutes.' },
    'snapshot': { cat: 'schema', short: 'What is recorded, and how epochs collapse.',
      body: '<p>Declared once and constant for the run. The framework takes the argmax of the scorer, gathers each declared layer from the winning observation, and writes the snapshot.</p>' },
    'reducer': { cat: 'plugin', short: 'A CUSTOM reduction. Optional, and rarely needed.',
      body: '<p>Do not read this as "reduction does not happen". <b>The reduce stage always runs</b> - it is one of the four stages, and it is what collapses your epochs into a delivered product.</p><p><b>Where is the reduction declared, then?</b> Per layer, in each <code>aggregate</code> block. This names a <i>plugin</i> that replaces that vocabulary with code, and it is for one thing: what the vocabulary cannot say. A joint vote across two categorical layers is the worked case - <code>manifest-joint.yaml</code> combines EMIT\u2019s two mineral groups, which no <code>aggregate</code> entry can express.</p>',
      ex: 'reducer:\n  ref: joint_mineral_vote\n  params: {prefer: mineral_2, fallback: mineral_1}',
      gotcha: 'A plugin REPLACES the schema-derived bands rather than adding to them: the schema still says what a snapshot holds, the plugin says what the product delivers. Its outputs cannot be rendered, either - `outputs.render` names a schema layer, and a plugin band is not one.',
      invalidates: 'Products only. Snapshots and every warp survive, because the schema and scorer did not move - measured on the worked example, adding the reducer re-ran 288 reduce items and hit all 1,803 snapshots.' },

    'outputs': { cat: 'output', short: 'Where products go and what they look like.',
      body: '<p><code>bucket</code>, <code>formats</code>, <code>stac</code>, and per-band <code>render</code>.</p>',
      gotcha: 'outputs.bucket is the ONLY thing that differs between a laptop run and a cloud one. Where work runs is a command-line choice, never a manifest field - which is what lets one file be the record of both.' },
    'budget': { cat: 'budget', short: 'What the run may cost. Required.',
      body: '<p>Checked <b>before</b> the planner opens or downloads anything, so an over-budget run costs nothing to discover.</p>',
      ex: 'budget:\n  max_tiles: 18\n  max_granules: 100\n  on_exceed: fail',
      gotcha: 'Required, not opt-in. A run states what it expects to cost and refuses to exceed it; on_exceed chooses between require_approval, fail and warn.' },
    'plugins': { cat: 'plugin', short: 'How your own code reaches the workers. Not built.',
      body: '<p>The <b>iteration</b> delivery path of 04 section 7: publish a wheel, rerun, skip the image build. Today it is modelled and refused.</p><p>The <b>production</b> path is what runs: a plugin is an ordinary Python distribution installed beside <code>stratum</code> and found through entry points, pinned in the container image and named by digest. <code>plugins/stratum-emit/</code> is that, which is why the path a third party takes is the one this repo takes.</p>',
      ex: 'plugins:\n  wheel: s3://bucket/stratum_emit-0.4.2-py3-none-any.whl',
      gotcha: 'Choosing among REGISTERED plugins is a manifest edit and needs no deploy. Adding a NEW class means a new image, because resolution reads installed metadata - about a two-minute round trip until this path exists.' },
    'plugins.wheel': { cat: 'plugin', short: 'A wheel to fetch at cold start. Refused: not built.',
      body: '<p>Would be fetched to <code>/tmp</code> when a worker starts cold, so an edited scorer ships in about 90 seconds instead of an image build.</p>',
      ex: 'wheel: s3://bucket/stratum_emit-0.4.2-py3-none-any.whl',
      gotcha: 'Refused at validation ON PURPOSE. The field is modelled and read by nothing, so a run would use whatever is installed and record a plugin version nobody chose - a silent substitution in delivered provenance. Refusing beats ignoring.',
      invalidates: 'Nothing today. Once built, the wheel\u2019s content hash would join plugin identity (06 section 3 rule 3) and move every key the plugin determines.' },
    'allow_mixed_vintage': { cat: 'structure', short: 'Opt out of the class-table agreement check.',
      body: '<p>Reprocessing shifts mineral classes. By default a run refuses granules whose embedded class tables disagree by fingerprint, because blending two vintages produces a result that looks entirely plausible.</p>',
      gotcha: 'Setting it true requires mixed_vintage_reason. The reason is recorded in provenance, so the decision is attributable rather than a flag somebody flipped.' },

    /* ---------------- grid ---------------- */
    'grid.crs': { cat: 'geometry', short: 'The coordinate reference system of the output grid.',
      body: '<p>Every unit below is in this system’s units. <code>EPSG:4326</code> means degrees.</p>', ex: 'crs: EPSG:4326' },
    'grid.resolution': { cat: 'geometry', short: 'Cell size, as [x, y].',
      body: '<p>In the CRS’s units. One arcsecond is <code>0.000277777777777778</code>.</p>',
      ex: 'resolution: [0.000277777777777778, -0.000277777777777778]',
      gotcha: 'y is NEGATIVE. Rows run north to south, so the y step is downward. A positive y is refused rather than silently flipping your product.' },
    'grid.origin': { cat: 'geometry', short: 'Where the lattice starts.',
      body: '<p>Fixes the phase of the grid, so two runs at the same resolution share cells rather than landing half a pixel apart.</p>', ex: 'origin: [-180, -90]' },
    'grid.tile_size': { cat: 'geometry', short: 'The edge of one tile, in CRS units.',
      body: '<p>A tile is the unit of publication: one product directory per tile per period.</p>', ex: 'tile_size: 0.5',
      gotcha: 'A tile INDEX is a count of tile_size steps, not degrees. At tile_size 0.5, tile -236 is longitude [-118.0, -117.5). They coincide only at tile_size 1.0, which is why -236_75 does not read as a place.' },
    'grid.block_size': { cat: 'geometry', short: 'Cells per block edge. The unit of work.',
      body: '<p>A block is what one worker resolves. Smaller blocks mean more parallelism and more overhead.</p>', ex: 'block_size: 450',
      invalidates: 'Snapshots and products, but NOT lookup tables or warps. block_size is excluded from the grid identity because it never changes output - but a snapshot is keyed on its block window, so renumbering the blocks renumbers the keys.' },
    'grid.max_distance': { cat: 'geometry', short: 'How far the regrid may reach for a pixel.',
      body: '<p>The KD-tree cutoff. A cell whose nearest sensor pixel lies beyond it is not filled. Defaults to 1.5 x the grid diagonal.</p>',
      invalidates: 'Every lookup table, and everything under them. This is geometry.' },
    'grid.regrid_method': { cat: 'geometry', short: 'How sensor pixels reach the grid.',
      body: '<p><code>kdtree</code> builds the lookup table from the granule’s own lat/lon. <code>adopt</code> crops the product’s own table, which requires it to already sit on this run’s lattice.</p>',
      invalidates: 'Every lookup table.' },

    /* ---------------- aoi ---------------- */
    'aoi.bbox': { cat: 'geometry', short: '[west, south, east, north]; the tiles follow.',
      body: '<p>Every tile whose nominal bounds intersect the box, ordered south to north then west to east.</p>', ex: 'bbox: [-118.0, 37.0, -116.5, 40.0]' },
    'aoi.tiles': { cat: 'geometry', short: 'Tile indices, named outright.',
      body: '<p>For when the tiling is the decision rather than the consequence.</p>', ex: 'tiles: [[-236, 74], [-235, 74]]' },
    'aoi.zones': { cat: 'geometry', short: 'Named boxes from a registry.',
      body: '<p>The form to use for anything delivered: runs stay comparable and the report names the zone.</p>', ex: 'zones: [cuprite-nv]\nregistry: ../zones.yaml' },
    'aoi.registry': { cat: 'geometry', short: 'Where zone names are resolved.',
      body: '<p>A manifest-relative YAML of <code>name: [w, s, e, n]</code>. Pairs with <code>zones</code>.</p>' },

    /* ---------------- time ---------------- */
    'time.start': { cat: 'time', short: 'Inclusive, UTC.', body: '<p>The beginning of the window granules are selected from.</p>' },
    'time.end': { cat: 'time', short: 'Exclusive, UTC.',
      body: '<p>Exclusive, so a calendar year is <code>2025-01-01</code> to <code>2026-01-01</code> and no granule is counted twice at a boundary.</p>',
      invalidates: 'The index scope. Widening the range means a rebuild, and the planner refuses an index built for less rather than silently planning against it.' },
    'time.epoch': { cat: 'time', short: 'The voting unit.',
      body: '<p>How finely observations are bucketed <b>before</b> anything is combined. <code>P1M</code> means one snapshot per calendar month, so a cell seen three times in March still casts one March vote.</p>', ex: 'epoch: P1M',
      gotcha: 'More epochs cost NO extra bandwidth - the same granules are partitioned more finely. What costs bandwidth is widening start and end.' },
    'time.deliver': { cat: 'time', short: 'How epochs collapse into a product.',
      body: '<p><code>P1Y</code> reduces every epoch in a year to one delivered product per tile. The long form is <code>{every, window, align}</code>; the short form normalises to it, so both hash the same.</p>', ex: 'deliver: P1Y',
      gotcha: 'Epoch and delivery answer different questions. Twelve monthly votes reduced to one annual answer is not the same as one annual epoch - the latter would have no votes to count.' },

    /* ---------------- inputs ---------------- */
    'inputs.index_location': { cat: 'binding', short: 'The directory holding the frozen index.',
      body: '<p>A run reads whatever index is here and never queries a catalogue. The file inside has a fixed name, so nobody renames it by hand.</p>' },
    'inputs.source': { cat: 'binding', short: 'How the index is BUILT. Ignored by a run.',
      body: '<p>Read by <code>stratum index build</code> only. <code>kind: cmr</code> searches NASA’s catalogue; <code>kind: local</code> walks a directory.</p>' },
    'patterns': { cat: 'binding', short: '{collection: {asset: glob}} - which files exist.',
      body: '<p>Matched against each catalogue record’s file names. <b>A file matching no glob is never indexed, and therefore can never be downloaded.</b></p>',
      ex: 'patterns:\n  EMITL1BRAD:\n    OBS: "EMIT_L1B_OBS_001_*.nc"',
      gotcha: 'This one rule is what keeps the 1.85 GB radiance file off the network - not a size check, not a special case. And the granule id is what the FIRST asset’s wildcards matched, so two collections globbed inconsistently produce ids that never merge.' },
    'inputs.roles': { cat: 'binding', short: 'collection + asset + var, under a name.',
      body: '<p>Of the files the index knows about, which one and which variable inside, named for plugins to use. The names here become the keys of <code>obs</code>.</p>',
      gotcha: 'You may index more than you read - that costs only metadata. You may not read what you did not index, and the planner refuses rather than letting a worker discover it.',
      invalidates: 'Every snapshot: which roles are read is part of the masked-observation key.' },
    'inputs.band_aliases': { cat: 'binding', short: 'One band of a role, under its own name.',
      body: '<p>A role reads a whole variable. EMIT’s <code>obs</code> is (downtrack, crosstrack, 11), so an alias names one band of it for a plugin to ask for by meaning.</p>',
      ex: 'band_aliases:\n  solar_zenith: {role: geometry, band: 4}',
      gotcha: 'An alias ADDS an entry; it never replaces the role. With no alias, obs["geometry"] is still there as the full (H, W, 11) array - nothing is auto-named, and there is no inference anywhere.' },
    'inputs.geolocation': { cat: 'binding', short: 'Which role builds the lookup table.',
      body: '<p>Defaults to the first sensor-space role. The table built from its per-pixel lat/lon is what decides which granules reach which block.</p>',
      gotcha: 'A run needs at least one sensor-space role. An all-ortho run is refused at plan time, because there would be nothing to build the table from.' },
    'inputs.readers': { cat: 'binding', short: 'Override which reader serves a collection.',
      body: '<p>A reader is selected from what a role <b>declares</b>, never from what a file is called.</p>' },
    'collection': { cat: 'binding', short: 'Which catalogue collection this role reads.',
      body: '<p>Also selects the reader. Names are the catalogue’s short names.</p>' },
    'asset': { cat: 'binding', short: 'Which file of the record.',
      body: '<p>A record can hold several files. Omit it and the collection’s primary asset is used - the first name in sorted order.</p>' },
    'var': { cat: 'binding', short: 'Which variable inside the file.',
      body: '<p>Named explicitly; nothing sniffs a filename. For a GeoTIFF this is the band’s description rather than an index.</p>' },
    'resampling': { cat: 'aux', short: 'How a raster is warped onto the block grid.',
      body: '<p>Required for an <b>ortho-native</b> role and for every aux raster; refused for a sensor-space role, which is gathered through a lookup table instead.</p>',
      gotcha: 'Declared, never defaulted - because bilinear-interpolating a class label produces a number that is not a class, and nothing downstream can detect it.',
      invalidates: 'The warps that use it, and every snapshot downstream.' },
    'class_table': { cat: 'binding', short: 'Where this product keeps its own class list.',
      body: '<p>Read from the granule being processed rather than a checked-in CSV, so it cannot drift from the pixels it describes.</p>',
      gotcha: 'Classes are matched on ATTRIBUTES, never on the integer. Raw values are positional and differ between vintages.' },

    /* ---------------- aux ---------------- */
    'uri': { cat: 'aux', short: 'Where the source is. One, or a list.',
      body: '<p>A global raster ships as a tile set, so an AOI wider than one tile needs several. They composite later-over-earlier, each contributing only where it has data.</p>',
      gotcha: 'The ORDER is part of the source’s identity - reorder them and it is a different artifact under a different key. And there is no globbing: discovering what a bucket holds is a listing, which a run may not do.',
      invalidates: 'Everything downstream of the source - its warps, then every snapshot that reads it. Identity is the content digest, so replacing a file in place under a stable URI invalidates correctly.' },
    'kind': { cat: 'aux', short: 'continuous or categorical.',
      body: '<p>Decides which resampling methods are legal and how nodata is represented.</p>',
      gotcha: 'A categorical source with a continuous resampling method is a plan-time error, not a runtime surprise.' },

    /* ---------------- snapshot ---------------- */
    'snapshot.name': { cat: 'schema', short: 'The schema’s name, carried into products.',
      body: '<p>Identifies the shape of what was written, for a consumer reading it later.</p>' },
    'layers': { cat: 'schema', short: 'What is recorded about the winning observation.',
      body: '<p>Each layer names a <code>source</code> - a role or a band alias - a <code>kind</code>, and an <code>aggregate</code>.</p>',
      invalidates: 'Changing WHAT a layer holds rebuilds every snapshot. Changing how it aggregates rebuilds only the products.' },
    'aggregate': { cat: 'schema', short: 'How epochs collapse for this layer. THIS is the reducer.',
      body: '<p><code>vote</code> for categorical, <code>median</code> / <code>percentile</code> / <code>inverse_variance</code> for continuous, <code>none</code> to carry a layer without delivering it.</p><p>If you went looking for a <code>reducer:</code> key and could not find one: this is it. Reduction is declared <b>per layer</b>, not once for the run, so a manifest can vote on a mineral and take a median of its depth in the same breath. The built-in reducer is this vocabulary being executed.</p>',
      ex: 'aggregate: {method: vote, min_count: 2, ignore: [none]}',
      invalidates: 'Products only. Snapshots survive, which makes this the cheapest thing in the file to experiment with.' },
    'method': { cat: 'schema', short: 'Which reduction. The full list is in the reference.',
      body: '<p>Categorical layers take <code>vote</code>, <code>best</code> or <code>none</code>. Continuous layers add <code>median</code>, <code>mean</code>, <code>min</code>, <code>max</code>, <code>percentile</code>, <code>score_weighted</code> and <code>inverse_variance</code>.</p><p>Which parameters each one takes is <a href="../reference/manifest.html#aggregate">tabulated in the manifest reference</a>, generated by probing the models rather than transcribed - so it cannot drift from what the validator accepts.</p>',
      gotcha: 'A parameter a method does not take is a load-time error naming the method, not something silently ignored. `percentile` requires `p`; `inverse_variance` requires `unc`.' },

    'min_count': { cat: 'schema', short: 'How many epochs must agree.',
      body: '<p>Suppresses a cell whose modal class has fewer votes than this.</p>',
      gotcha: 'It needs that many SEPARATE epochs, so it interacts with time.epoch more than it looks. Measured on the worked example, going from 8 monthly epochs to 12 tripled the vote rate - adding months adds pairs, not evidence linearly.' },
    'ignore': { cat: 'schema', short: 'Classes excluded from the tally, by name.',
      body: '<p><code>none</code> is the reserved "observed, nothing identified" class - real information, and usually not a vote.</p>',
      gotcha: 'By name, never by integer. -9999 is "not observed" and 0 is "observed, nothing identified"; conflating them fabricates agreement.' },
    'tie_break': { cat: 'schema', short: 'What happens when votes tie.',
      body: '<p><code>earliest</code>, <code>latest</code>, <code>highest_score</code> or <code>nodata</code>.</p>' },
    'classes': { cat: 'schema', short: 'The enumeration a categorical layer resolves into.',
      body: '<p><code>source</code> uses the granules’ own table. A <code>@ref:</code> points at a curated enumeration - the product’s vocabulary, and its legend.</p>' },

    /* ---------------- outputs ---------------- */
    'bucket': { cat: 'output', short: 'The storage root. A directory, or s3://.',
      body: '<p><code>assets/</code>, <code>cache/</code>, <code>runs/</code> and <code>products/</code> all hang off it.</p>',
      gotcha: 'The only field that differs between a laptop run and a cloud one.' },
    'formats': { cat: 'output', short: 'cog, gtiff, or both.',
      body: '<p><code>cog</code> for delivery - header-first with an overview pyramid. <code>gtiff</code> for something you want to open locally.</p>' },
    'stac': { cat: 'output', short: 'Write a STAC item per product.',
      body: '<p>The products tree becomes a STAC catalogue, which is the viewer’s only contract - a run that delivers a new band needs no viewer change.</p>' },
    'render': { cat: 'output', short: 'Per-band RGBA images and their legends.',
      body: '<p>Presentation only, and the one thing you can change without re-running the pipeline.</p>',
      invalidates: 'Nothing. Re-renders from published data.' },

    /* ---------------- budget ---------------- */
    'max_tiles': { cat: 'budget', short: 'Ceiling on tiles.', body: '<p>Checked against the tiles the AOI resolves to.</p>' },
    'max_granules': { cat: 'budget', short: 'Ceiling on granules after filtering.', body: '<p>The one that usually binds, because granule count grows sublinearly with area.</p>' },
    'max_vcpu_hours': { cat: 'budget', short: 'Ceiling on compute.', body: '<p>Not estimated in this slice, so it does not currently bind.</p>' },
    'on_exceed': { cat: 'budget', short: 'require_approval, fail or warn.',
      body: '<p>What happens when a ceiling is crossed. <code>require_approval</code> pauses the run for a human.</p>' },

    /* ---------------- filters ---------------- */
    'max_cloud_fraction': { cat: 'plugin', short: 'Drop a granule cloudier than this.',
      body: '<p>A fraction in [0, 1], matched against the catalogue’s scene-level cloud cover.</p>',
      gotcha: 'It is SCENE level. An 85%-cloudy granule may still hold the only clear look at a cell, and this throws all of it away. Filter coarsely for cost, then mask per pixel for correctness - a granule filter is not a cloud mask.' },
    'max_solar_zenith': { cat: 'plugin', short: 'Drop a granule acquired with the sun too low.',
      body: '<p>Degrees. Low sun means long shadows and poor illumination.</p>' },
    'month_in': { cat: 'plugin', short: 'A recurring seasonal window.',
      body: '<p>Months by number, and deliberately not a date range: the useful window recurs every year. August to November across the Western US avoids peak vegetation without discarding whole years.</p>',
      ex: 'month_in: [8, 9, 10, 11]',
      gotcha: 'start/end cannot express this, and building it from many disjoint intervals is miserable. That is why it is its own predicate.' },
    'on_missing': { cat: 'plugin', short: 'What to do when a granule lacks the value.',
      body: '<p><code>reject</code>, <code>keep</code> or <code>fail</code>.</p>',
      gotcha: 'Defaults to fail, on purpose. A granule whose record lacks the key is indistinguishable from one that failed the test, so silently dropping it is data loss with no signal - which is exactly what the pipeline this replaces did.' },
    'version': { cat: 'binding', short: 'Pin a role to one collection version.',
      body: '<p>Required when the index holds a collection under several versions, so a run cannot straddle two.</p>' },

    /* ---------------- aux, temporal ---------------- */
    'temporal': { cat: 'aux', short: 'How a date-keyed source is resolved. Not built.',
      body: '<p><code>nearest</code>, <code>previous</code> or <code>epoch</code>, against a <code>{date}</code> placeholder in the URI.</p>',
      gotcha: 'Refused at validation. "The nearest date that exists" needs a listing of what exists, and a run never queries a catalogue - honouring it means freezing an available-date list into the plan, which is a feature nobody needs yet.' },
    'max_age': { cat: 'aux', short: 'How stale a resolved date may be.',
      body: '<p>Pairs with <code>temporal: nearest</code> or <code>previous</code>. Not built, for the same reason.</p>' },

    /* ---------------- aggregation detail ---------------- */
    'conditional_on': { cat: 'schema', short: 'Aggregate only over the epochs that voted for the winner.',
      body: '<p>A continuous layer conditioned on a categorical one: the median depth <em>of the months that agreed</em>, rather than of all of them.</p>',
      ex: 'aggregate: {method: median, conditional_on: mineral_1}',
      gotcha: 'It must name another categorical layer that is actually delivered. Conditioning on a carried layer - one with aggregate none - has no winner to condition on, and fails at plan time rather than producing a plausible average.' },
    'spread': { cat: 'schema', short: 'Also record the dispersion.',
      body: '<p><code>iqr</code> or <code>stddev</code>, written as a companion band. How much the epochs disagreed is usually as interesting as the answer.</p>' },
    'unc': { cat: 'schema', short: 'Which layer carries this one’s uncertainty.',
      body: '<p>Names another continuous layer, used as weights by <code>inverse_variance</code>.</p>' },

    /* ---------------- render ---------------- */
    'mapper': { cat: 'output', short: 'How a band becomes an image.',
      body: '<p><code>categorical</code> colours each class from the legend; <code>continuous</code> ramps a range.</p>' },
    'on_unmapped': { cat: 'output', short: 'What to do with a class the legend does not name.',
      body: '<p><code>grey</code> draws it neutrally; <code>fail</code> refuses to publish.</p>',
      gotcha: 'grey is the honest default for an exploratory run - a class nobody assigned a colour is still a real identification, and dropping it would quietly understate coverage.' },
    'alpha_from': { cat: 'output', short: 'Drive transparency from another band.',
      body: '<p>Fades cells by a second band - typically agreement, so where the epochs disagreed the image says so.</p>',
      ex: 'alpha_from: {band: mineral_1_agreement, domain: [0.25, 1.0], range: [60, 255]}',
      gotcha: 'The band must be one the reducer actually delivers, which is checked at plan time rather than at publish.' },
    'domain': { cat: 'output', short: 'The input range of a ramp or alpha map.',
      body: '<p>Two numbers, in the band’s own units.</p>' },
    'range': { cat: 'output', short: 'The output range of an alpha map.',
      body: '<p>Two alpha values, 0 to 255.</p>' },

    'plugins': { cat: 'plugin', short: 'Where plugin code comes from. Not built.',
      body: '<p>Points a run at a wheel rather than at whatever the image happens to contain, so an experiment can change a scorer without rebuilding and redeploying.</p>',
      gotcha: 'Not built. Today a plugin reaches a deployment by being installed into the image, which means the image digest identifies the code that ran - a property a wheel URI would have to earn back with a content hash.' },

    /* ---------------- plugin refs ---------------- */
    'ref': { cat: 'plugin', short: 'Which plugin. An entry-point name or module:Class.',
      body: '<p>Resolved from installed metadata, so a plugin is a distribution rather than a file path. <code>stratum plugins list</code> shows what an environment provides.</p>' },
    'params': { cat: 'plugin', short: 'Constructor arguments for the plugin.',
      body: '<p>Validated at plan time by actually constructing it, so a bad parameter fails before compute is provisioned.</p>',
      invalidates: 'Whatever the plugin determines. Scorer params rebuild snapshots; mapper params re-render only.' }
  };

  /* ---- rendering ---------------------------------------------------- */
  function esc(t) {
    return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* A manifest is scanned line by line. A key at indent 0 becomes the current
     section, so `name` under `snapshot` can mean something different from `name`
     anywhere else; lookup tries "section.key" and falls back to the bare key. */
  function lookup(section, key) {
    if (section && DICT[section + '.' + key]) { return section + '.' + key; }
    return DICT[key] ? key : null;
  }

  function render(name) {
    var text = MANIFESTS[name].text;
    var lines = text.split('\n');
    var section = '';
    var html = '';

    lines.forEach(function (line, i) {
      var num = String(i + 1);
      var body = '';
      var rest = line;

      /* split off a trailing comment so keys inside it are not linkified */
      var cut = rest.indexOf('#');
      var comment = '';
      if (cut !== -1) { comment = rest.slice(cut); rest = rest.slice(0, cut); }

      var top = rest.match(/^([a-z_]+):/);
      if (top) { section = top[1]; }

      body = esc(rest).replace(/([a-z_][a-z0-9_]*)(\s*:)/g, function (m, key, colon) {
        var k = lookup(section, key);
        if (!k) { return m; }
        var cls = CAT[DICT[k].cat][1];
        return '<span class="tok ' + cls + '" data-k="' + k + '" tabindex="0" role="button">'
             + key + '</span>' + colon;
      });

      if (comment) { body += '<span class="cmt">' + esc(comment) + '</span>'; }
      /* No trailing newline: .ln is display:block, and inside a <pre> a newline after a block
         renders as a SECOND line break. One or the other, never both. */
      html += '<span class="ln" data-n="' + num + '">' + (body || '&nbsp;') + '</span>';
    });

    document.getElementById('dec-code').innerHTML = html;
    document.getElementById('dec-file').textContent = MANIFESTS[name].path;
    document.getElementById('dec-note').textContent = MANIFESTS[name].note || '';
  }

  function show(k) {
    var e = DICT[k];
    if (!e) { return; }
    var cat = CAT[e.cat];
    document.getElementById('dec-cat').textContent = cat[0];
    document.getElementById('dec-cat').className = 'dec-cat ' + cat[1];
    document.getElementById('dec-tok').textContent = k.indexOf('.') === -1 ? k : k.split('.')[1];

    var h = '<p class="dec-short">' + e.short + '</p>' + e.body;
    if (e.ex) { h += '<pre class="dec-ex"><code>' + esc(e.ex) + '</code></pre>'; }
    var flags = '';
    if (e.gotcha) { flags += '<div class="dec-flag dec-gotcha"><span>The gotcha</span><p>' + e.gotcha + '</p></div>'; }
    if (e.invalidates) { flags += '<div class="dec-flag dec-inval"><span>Changing it rebuilds</span><p>' + e.invalidates + '</p></div>'; }
    if (flags) { h += '<div class="dec-flags">' + flags + '</div>'; }
    document.getElementById('dec-body').innerHTML = h;
  }

  /* ---- the schema skeleton above the decoder -------------------------
   * Same categories, same colours, so the overview and the real file read as one
   * thing. Colour only, not clickable: the panel lives below the decoder, and a
   * token up here that scrolled you away from what you were reading would be a
   * worse affordance than none.
   */
  /* The skeleton is flat text with no section to scope a lookup, so `crs` has to find
     `grid.crs`. Built once, first entry wins - the qualified keys are unique by last segment. */
  var BARE = {};
  Object.keys(DICT).forEach(function (k) {
    var last = k.indexOf('.') === -1 ? k : k.split('.')[1];
    if (!BARE[last]) { BARE[last] = k; }
  });

  function colourise(block) {
    if (!block) { return; }
    var walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, null);
    var texts = [];
    while (walker.nextNode()) {
      /* skip anything already marked up - the comment spans */
      if (!walker.currentNode.parentElement.closest('.c')) { texts.push(walker.currentNode); }
    }
    texts.forEach(function (node) {
      var out = esc(node.nodeValue).replace(/\b([a-z_][a-z0-9_]*)\b/g, function (m, word) {
        var k = BARE[word];
        if (!k) { return m; }
        return '<span class="tok-static ' + CAT[DICT[k].cat][1] + '">' + word + '</span>';
      });
      if (out === esc(node.nodeValue)) { return; }
      var span = document.createElement('span');
      span.innerHTML = out;
      node.parentNode.replaceChild(span, node);
    });
  }

  /* ---- wiring -------------------------------------------------------- */
  var picker = document.getElementById('dec-picker');
  Object.keys(MANIFESTS).forEach(function (name, i) {
    var b = document.createElement('button');
    b.textContent = MANIFESTS[name].label;
    b.setAttribute('data-m', name);
    if (i === 0) { b.className = 'active'; }
    picker.appendChild(b);
  });

  picker.addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-m]');
    if (!b) { return; }
    [].forEach.call(picker.children, function (c) { c.className = ''; });
    b.className = 'active';
    render(b.getAttribute('data-m'));
  });

  function activate(el) {
    var k = el.getAttribute('data-k');
    var cur = root.querySelector('.tok.on');
    if (cur) { cur.classList.remove('on'); }
    el.classList.add('on');
    show(k);
  }

  root.addEventListener('click', function (ev) {
    var t = ev.target.closest('.tok');
    if (t) { activate(t); }
  });
  root.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') { return; }
    var t = ev.target.closest && ev.target.closest('.tok');
    if (t) { ev.preventDefault(); activate(t); }
  });

  render(Object.keys(MANIFESTS)[0]);
  colourise(document.getElementById('schema-skeleton'));
  root.classList.add('ready');
}());
