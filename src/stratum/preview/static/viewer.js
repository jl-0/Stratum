/* The Stratum preview viewer.
 *
 * Served by `stratum preview`, locally or from the Fargate task. Rasters are rendered by the
 * server; this file draws the result, the legend and the controls, and does no colour arithmetic
 * of its own - the ramps come from /api/config, so there is exactly one colour table in the
 * repository and it is the one in `render.py`.
 *
 * Nothing here knows the products layout. It reads collection.json, follows the item links, and
 * draws whatever assets the items declare - so a run that publishes a new band appears in the
 * layer list without this file changing.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const state = { products: '', runs: [], run: null, items: [], layer: null, legend: null,
                overlays: [], grid: null, ramps: {}, legends: {} };

/** The raster layer for one asset: an XYZ layer over the server's tile endpoint. The style
 *  travels in the query string, so the server holds no session. */
function rasterLayer(asset, opts) {
  const query = new URLSearchParams(
    opts.kind === 'categorical' ? { kind: 'categorical', legend: opts.legendHref }
    : opts.kind === 'rgba' ? { kind: 'rgba' }
    : { kind: 'continuous', ramp: opts.ramp });
  return L.tileLayer(`tiles/${asset}/{z}/{x}/{y}.png?${query}`,
                     { opacity: opts.opacity, bounds: opts.bounds, maxNativeZoom: 16 });
}

/** A 256-row lookup table for a named ramp, interpolated from the nine stops the server sent.
 *  Used only to draw the legend's gradient - the tiles are already coloured when they arrive. */
const lutCache = {};
function rampLUT(name) {
  if (lutCache[name]) return lutCache[name];
  const stops = state.ramps[name] || Object.values(state.ramps)[0];
  if (!stops || stops.length < 2) return null;    // no ramps declared: draw no gradient
  const step = 255 / (stops.length - 1);
  const lut = [];
  for (let i = 0; i < 256; i++) {
    const t = i / step, a = stops[Math.floor(t)], b = stops[Math.min(stops.length - 1, Math.ceil(t))];
    const f = t - Math.floor(t);
    const mix = [];
    for (let k = 0; k < 3; k++) mix.push(Math.round(a[k] + (b[k] - a[k]) * f));
    lut.push(mix);
  }
  return (lutCache[name] = lut);
}


/* ------------------------------------------------------------------ the map */

const map = L.map('map', { zoomControl: true, attributionControl: true })
  .setView([37.5, -117.2], 8);
L.control.scale({ imperial: false }).addTo(map);

const BASEMAPS = {
  osm: () => L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }),
  esri: () => L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Imagery &copy; Esri' }),
  none: () => null,
};
let base = null;
function setBasemap(name) {
  if (base) map.removeLayer(base);
  base = BASEMAPS[name]();
  if (base) base.addTo(map);
  if (base) base.bringToBack();
}

/* ------------------------------------------------------------------ loading */

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.json();
}

const say = (text, bad) => {
  const el = $('status');
  el.textContent = text || '';
  el.classList.toggle('err', !!bad);
};

/** Read a run: its collection, then every item in parallel. */
async function loadRun(runId) {
  say('reading the catalogue…');
  const collection = await fetchJSON(`stac/${runId}/collection.json`);
  const hrefs = collection.links.filter((l) => l.rel === 'item')
    .map((l) => `${runId}/${l.href.replace(/^\.\//, '')}`);
  const items = (await Promise.all(hrefs.map(async (href) => {
    try {
      const item = await fetchJSON(`stac/${href}`);
      const dir = href.replace(/[^/]*$/, '');
      for (const a of Object.values(item.assets)) a.href = dir + a.href.replace(/^\.\//, '');
      return item;
    } catch { return null; }              // a half-published run is a gap, not a failure
  }))).filter(Boolean);

  state.run = runId; state.items = items;
  const p = collection.extent?.temporal?.interval?.[0] ?? [];
  $('runmeta').textContent =
    `${items.length} tile${items.length === 1 ? '' : 's'}` +
    (p[0] ? ` · ${p[0].slice(0, 10)} to ${(p[1] || '').slice(0, 10)}` : '');

  fillLayers(items);
  say(items.length ? '' : 'this run has no readable items', !items.length);
}

/** The layer picker: the union of the items' data and visual assets, in publication order. */
function fillLayers(items) {
  const seen = new Map();
  for (const item of items) {
    for (const [key, asset] of Object.entries(item.assets || {})) {
      const roles = asset.roles || [];
      if (!roles.includes('data') && !roles.includes('visual')) continue;
      if (!seen.has(key)) seen.set(key, asset);
    }
  }
  const select = $('layer');
  const previous = state.layer;
  select.innerHTML = '';
  for (const [key, asset] of seen) {
    const option = document.createElement('option');
    option.value = key;
    option.textContent = asset.title || key;
    select.append(option);
  }
  state.layer = seen.has(previous) ? previous : [...seen.keys()][0] ?? null;
  select.value = state.layer ?? '';
  drawLayer();
}

/* ------------------------------------------------------------------ drawing */

/* ------------------------------------------- is it still drawing, or is it actually empty?

   A blank patch of map means one of two things - not fetched yet, or nothing published there -
   and they are pixel-identical. This tracker is the difference, and it earns its keep because
   the answer is usually "no data": the tile endpoint returns a TRANSPARENT PNG when a request
   falls outside the raster rather than a 404, so once the outstanding count reaches zero a gap
   is real. That is the one sentence the viewer could not say before.

   "map tiles" throughout, never just "tiles": in this product a tile is a one-degree cell, and
   the panel already has a "Show tile outlines" control for those.
*/
const drawing = { pending: 0, drawn: 0, failed: 0, phase: 'idle', settle: 0, frame: 0 };

/** Begin a phase. `preparing` covers the legend and range lookups, which happen before any
 *  map tile is requested and used to look like a hang. */
function drawingReset(phase) {
  drawing.pending = drawing.drawn = drawing.failed = 0;
  drawing.phase = phase;
  clearTimeout(drawing.settle);
  drawing.settle = 0;
  paintDrawing();
}

/** Leaflet fires these in bursts of dozens, so coalesce rather than repaint per tile.
 *
 * A timer, NOT `requestAnimationFrame`: rAF is paused in a background tab, so a pill updated
 * that way freezes mid-load and is still wrong when you come back to it - which is precisely
 * the moment someone checks whether a blank patch has finished drawing. Measured: the pill sat
 * on "preparing the layer…" with two map tiles already drawn. */
function drawingTick() {
  if (drawing.frame) return;
  drawing.frame = setTimeout(() => { drawing.frame = 0; paintDrawing(); }, 50);
}

/** Count one map tile in flight, and re-enter `loading` when a pan or zoom asks for more. */
function watchTiles(layer) {
  layer.on('tileloadstart', () => {
    drawing.pending++;
    if (drawing.phase !== 'loading') { clearTimeout(drawing.settle); drawing.phase = 'loading'; }
    drawingTick();
  });
  layer.on('tileload', () => { drawing.pending--; drawing.drawn++; drawingTick(); });
  layer.on('tileerror', () => { drawing.pending--; drawing.failed++; drawingTick(); });
  return layer;
}

function paintDrawing() {
  const el = $('tilestatus');
  if (!el) return;
  const { pending, drawn, failed, phase } = drawing;
  if (phase === 'idle') { el.hidden = true; return; }
  el.hidden = false;
  el.classList.remove('settled', 'bad');

  if (phase === 'preparing') {
    el.innerHTML = '<span class="spinner"></span><span>preparing the layer…</span>';
    return;
  }
  if (pending > 0) {
    el.innerHTML = `<span class="spinner"></span><span>drawing — ${pending} map tile`
      + `${pending === 1 ? '' : 's'} outstanding</span>`;
    // Nothing is settled while requests are in flight; the timer is armed only on reaching zero.
    return;
  }
  if (phase === 'loading') {
    // Wait before declaring it done: Leaflet starts the next batch a beat after finishing one,
    // and without this the pill flickers between drawing and settled on every pan.
    if (!drawing.settle) {
      drawing.settle = setTimeout(() => {
        drawing.settle = 0;
        drawing.phase = 'settled';
        paintDrawing();
      }, 250);
    }
    el.innerHTML = '<span class="spinner"></span><span>drawing…</span>';
    return;
  }

  // Settled. This message is the point of the whole thing, so it stays on screen rather than
  // fading - a pill that disappears leaves the original ambiguity behind.
  if (failed) {
    el.classList.add('bad');
    el.innerHTML = `<span>${failed} map tile${failed === 1 ? '' : 's'} failed to load — `
      + 'a blank area may be a failed request, not missing data. Reload to retry.</span>';
    return;
  }
  el.classList.add('settled');
  el.innerHTML = `<span>drawn (${drawn} map tile${drawn === 1 ? '' : 's'}) — `
    + 'a blank area here is no data, not a pending load</span>';
}

/** One overlay per item, because one raster per tile is how the products are written. */
async function drawLayer() {
  for (const l of state.overlays) map.removeLayer(l);
  state.overlays = [];
  const key = state.layer;
  if (!key) { $('legend').innerHTML = ''; drawingReset('idle'); return; }
  drawingReset('preparing');

  const first = state.items.find((i) => i.assets?.[key]);
  const asset = first?.assets[key];
  $('layermeta').textContent = asset?.description || '';

  const roles = asset?.roles || [];
  const opts = { opacity: $('opacity').value / 100, ramp: $('ramp').value || 'viridis' };
  if (roles.includes('visual')) {
    opts.kind = 'rgba';
  } else {
    const legend = await legendFor(first, key);
    if (legend?.kind === 'categorical') {
      Object.assign(opts, { kind: 'categorical', entries: legend.entries,
                            legendHref: first.assets[`${key}_legend`].href });
    } else {
      opts.kind = 'continuous';
      Object.assign(opts, await measure(asset.href));
    }
    state.legend = legend;
  }
  $('ramprow').hidden = opts.kind !== 'continuous';

  for (const item of state.items) {
    const a = item.assets?.[key];
    if (!a) continue;
    const [w, s, e, n] = item.bbox;
    const layer = watchTiles(rasterLayer(a.href, { ...opts,
                                                   bounds: L.latLngBounds([s, w], [n, e]) }));
    layer.addTo(map);
    state.overlays.push(layer);
  }
  // No overlay means no map tile will ever be requested, so nothing would move it off
  // `preparing`. Say it plainly instead of spinning for ever.
  drawing.phase = state.overlays.length ? 'loading' : 'settled';
  paintDrawing();       // unconditionally: a coalesced repaint may still be in flight, and the
                        // phase just changed under it
  renderLegend(opts);
}

/** A continuous layer's range: the same stretch the tiles are rendered with. The server measures
 *  it once and caches it, so this costs nothing after the first layer change. Falling back to
 *  0..1 keeps a layer drawing rather than vanishing when the measurement fails. */
async function measure(href) {
  try {
    return await fetchJSON(`api/range?asset=${encodeURIComponent(href)}`);
  } catch {
    return { vmin: 0, vmax: 1 };
  }
}

function renderLegend(opts) {
  const el = $('legend');
  if (opts.kind === 'categorical' && state.legend) {
    const rows = state.legend.entries.filter((e) => e.id !== 0);
    el.innerHTML = `<h2>${state.layer} — ${rows.length} classes</h2>` +
      '<input id="legendfilter" type="search" placeholder="filter classes" ' +
      'aria-label="Filter classes">' +
      '<p class="muted">Or click the map: it reads every band at that point.</p><ul>' +
      rows.map((e) =>
        `<li data-name="${escapeHTML(e.name.toLowerCase())}">` +
        `<span class="sw" style="background:rgb(${e.color.join(',')})"></span>` +
        `<span class="name">${escapeHTML(e.name)}</span>` +
        `<span class="count">${e.id}</span></li>`).join('') + '</ul>';
    // A 294-row list is reference material; typing is faster than scrolling it.
    $('legendfilter').addEventListener('input', (ev) => {
      const needle = ev.target.value.trim().toLowerCase();
      for (const li of el.querySelectorAll('li')) {
        li.hidden = needle !== '' && !li.dataset.name.includes(needle);
      }
    });
  } else if (opts.kind === 'continuous') {
    const lut = rampLUT(opts.ramp);
    const stops = lut
      ? lut.filter((_, i) => i % 32 === 0).map((c) => `rgb(${c.join(',')})`).join(',') : '';
    const lo = opts.vmin ?? 0, hi = opts.vmax ?? 1;
    el.innerHTML = `<h2>${state.layer}</h2>` +
      (stops ? `<div class="bar" style="background:linear-gradient(to right,${stops})"></div>` : '') +
      `<div class="ticks"><span>${fmt(lo)}</span><span>${fmt(hi)}</span></div>` +
      '<p class="muted">Stretched to the 2nd–98th percentile of the band.</p>';
  } else {
    el.innerHTML = `<h2>${state.layer}</h2><p class="muted">A published rendering: its colours ` +
      'are the ones publish wrote into the image.</p>';
  }
}

const fmt = (v) => (Math.abs(v) >= 100 || v === 0 ? String(Math.round(v)) : v.toPrecision(3));
const escapeHTML = (s) => s.replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/** The tile outlines, drawn from the items' own bboxes - the grid this run was actually cut on. */
function drawGrid() {
  if (state.grid) map.removeLayer(state.grid);
  state.grid = null;
  if (!$('tiles').checked || !state.items.length) return;
  state.grid = L.layerGroup(state.items.flatMap((item) => {
    const [w, s, e, n] = item.bbox;
    const bounds = L.latLngBounds([s, w], [n, e]);
    const tile = item.properties?.['stratum:tile'];
    const box = L.rectangle(bounds, { color: '#9a5b2c', weight: 1, fill: false, dashArray: '3 3' });
    const label = L.marker(bounds.getNorthWest(), {
      interactive: false,
      icon: L.divIcon({ className: '', html: `<span class="tile-label">${tile ? tile.join(', ') : item.id}</span>` }),
    });
    return [box, label];
  })).addTo(map);
}

function fitRun() {
  if (!state.items.length) return;
  const bounds = state.items.reduce((acc, i) => {
    const [w, s, e, n] = i.bbox;
    return acc ? acc.extend(L.latLngBounds([s, w], [n, e])) : L.latLngBounds([s, w], [n, e]);
  }, null);
  map.fitBounds(bounds, { paddingTopLeft: [10, 10], paddingBottomRight: [330, 10] });
}

/* ------------------------------------------------------------ reading a point */

/** One asset's legend, fetched once and kept. A click reads every band, so the same legend is
 *  wanted again the moment the layer changes. */
async function legendFor(item, key) {
  const asset = item.assets?.[`${key}_legend`];
  if (!asset) return null;
  if (!(asset.href in state.legends)) {
    state.legends[asset.href] = await fetchJSON(`stac/${asset.href}`).catch(() => null);
  }
  return state.legends[asset.href];
}

/** What a band's values mean, when they mean a class.
 *
 * Published items carry the STAC Classification extension, and every categorical band declares
 * its own `classification:classes` - `mineral_1` and `mineral_1_runner_up` do, `mineral_1_agreement`
 * does not. So the asset says whether it is categorical, and a name is never inferred from one.
 * Guessing it from the asset key would put a class name on a fraction.
 */
const classesOf = (item, key) => item.assets?.[key]?.['classification:classes'] || null;

/** Which published tile covers a point. Items do not overlap, so the first hit is the answer. */
const itemAt = (latlng) => state.items.find((item) => {
  const [w, s, e, n] = item.bbox;
  return latlng.lng >= w && latlng.lng <= e && latlng.lat >= s && latlng.lat <= n;
});

/** Click anywhere: report every band at that point, not just the layer being drawn.
 *
 * This is what makes a 294-class legend usable - you stop hunting for a colour and ask the map
 * what it is. The raw value is shown beside the name because they are different facts: the name
 * comes from the legend, the number is what is in the pixel.
 */
async function readPoint(latlng) {
  const item = itemAt(latlng);
  if (!item) return;

  const keys = Object.keys(item.assets).filter((k) => {
    const roles = item.assets[k].roles || [];
    return roles.includes('data') || roles.includes('visual');
  });
  const query = new URLSearchParams({ lon: latlng.lng, lat: latlng.lat });
  for (const k of keys) query.append('asset', item.assets[k].href);

  const popup = L.popup({ maxWidth: 360, className: 'read' })
    .setLatLng(latlng).setContent('<p class="muted">reading…</p>').openOn(map);

  let reading;
  try {
    reading = await fetchJSON(`api/value?${query}`);
  } catch (e) {
    return popup.setContent(`<p class="err">${escapeHTML(e.message)}</p>`);
  }

  const rows = [];
  for (const key of keys) {
    const values = reading.values[item.assets[key].href];
    rows.push(`<tr><th>${escapeHTML(key)}</th><td>${describe(item, key, values)}</td></tr>`);
  }
  const tile = item.properties?.['stratum:tile'];
  popup.setContent(
    `<table class="read">${rows.join('')}</table>` +
    `<p class="muted">${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}` +
    (tile ? ` · tile ${tile.join(', ')}` : '') + '</p>');
}

/** One band's value, as a reader wants it: the class name where there is one, the number always,
 *  and the two kinds of absence kept apart. */
function describe(item, key, values) {
  if (values === null || values === undefined) return '<span class="muted">not observed</span>';
  if (values.length > 1) {                                   // an RGBA rendering
    const [r, g, b, a] = values;
    return a === 0 ? '<span class="muted">transparent</span>'
      : `<span class="sw" style="background:rgb(${r},${g},${b})"></span> ${r}, ${g}, ${b}`;
  }
  const value = values[0];
  if (value === null) return '<span class="muted">not observed</span>';

  const classes = classesOf(item, key);
  if (classes) {
    // 0 is `none` - observed, nothing identified - which is a different fact from not observed.
    if (value === 0) return '<span class="muted">none — observed, nothing identified</span>';
    const entry = classes.find((c) => c.value === value);
    if (!entry) {
      return `<span class="err">${value}</span> <span class="muted">— no such class in this ` +
             'product\u2019s table</span>';
    }
    const swatch = entry.color_hint
      ? `<span class="sw" style="background:#${entry.color_hint}"></span> ` : '';
    return `${swatch}${escapeHTML(entry.name)} <span class="muted">(${value})</span>`;
  }
  return Number.isInteger(value) ? String(value) : value.toPrecision(4);
}

/* ------------------------------------------------------------------ wiring */

map.on('click', (e) => readPoint(e.latlng));

$('run').addEventListener('change', async (e) => {
  await loadRun(e.target.value); drawGrid(); fitRun();
});
$('layer').addEventListener('change', (e) => { state.layer = e.target.value; drawLayer(); });
$('ramp').addEventListener('change', drawLayer);
$('tiles').addEventListener('change', drawGrid);
$('basemap').addEventListener('change', (e) => setBasemap(e.target.value));
$('opacity').addEventListener('input', (e) => {
  const o = e.target.value / 100;
  $('opacityval').textContent = `${e.target.value}%`;
  for (const l of state.overlays) {
    if (l.setOpacity) l.setOpacity(o);
    else l.eachLayer?.((sub) => sub.setOpacity?.(o));
  }
});

async function start() {
  const config = await fetchJSON('api/config');
  state.products = config.products;
  state.ramps = config.ramps;
  // The first ramp the server declares is the default, so the order in `render.RAMPS` is the
  // one that matters - not alphabetical.
  $('ramp').innerHTML = Object.keys(config.ramps).map((r) => `<option>${r}</option>`).join('');
  $('root').innerHTML = `<code>${escapeHTML(state.products)}</code>`;
  setBasemap($('basemap').value);

  // ?run=<id> opens one run directly, which is how you send someone a link to a specific result.
  const asked = new URLSearchParams(location.search).get('run');
  state.runs = (await fetchJSON('api/runs')).runs;
  if (asked && !state.runs.includes(asked)) state.runs.push(asked);
  if (!state.runs.length) return say('no published runs under this products root', true);

  $('run').innerHTML = state.runs.map((r) => `<option>${escapeHTML(r)}</option>`).join('');
  // Newest last by name, and a run id carries its date - so the last one is the useful default.
  $('run').value = asked && state.runs.includes(asked) ? asked : state.runs[state.runs.length - 1];
  await loadRun($('run').value);
  drawGrid(); fitRun();
}

start().catch((e) => say(e.message, true));
