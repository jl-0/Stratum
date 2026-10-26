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
 *
 * Two kinds of drawn layer, one shape. The PREVIEW is what the pickers on the right point at, and
 * is replaced whenever they change. The STACK is the layers someone pinned with "Add as layer":
 * each keeps the run it came from, so the pickers can move on to another run without losing it.
 * Every layer gets a Leaflet pane of its own, and that pane is the unit of stacking, visibility,
 * opacity and the peek-through mask - one CSS property each, however many overlays it holds.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const state = { products: '', runs: [], run: null, items: [], mosaics: [], bbox: null,
                layer: null, preview: null, stack: [], selected: null, drawSeq: 0, tool: null,
                grid: null, ramps: {}, legends: {} };

/** The raster layer for one asset: an XYZ layer over the server's tile endpoint. The style
 *  travels in the query string, so the server holds no session.
 *
 *  A continuous layer sends the SAME vmin/vmax for every product tile - the layer's range, not
 *  the tile's. Without them the server used to stretch each one-degree tile to its own range,
 *  and the tile grid showed through as boxes in a layer that has no tile edges. */
function rasterLayer(asset, opts) {
  const query = new URLSearchParams(
    opts.kind === 'categorical' ? { kind: 'categorical', legend: opts.legendHref }
    : opts.kind === 'rgba' ? { kind: 'rgba' }
    : { kind: 'continuous', ramp: opts.ramp, vmin: opts.vmin, vmax: opts.vmax });
  // Opacity is not set here: it belongs to the layer's pane, so one slider covers a layer drawn
  // as one mosaic and a layer drawn as four hundred per-tile overlays alike.
  return L.tileLayer(`tiles/${asset}/{z}/{x}/{y}.png?${query}`,
                     { bounds: opts.bounds, maxNativeZoom: 16, pane: opts.pane });
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

  // The run's layer mosaics (`stratum mosaic`): collection assets with the `overview` role, one
  // per layer and period. A layer that has one is drawn from it alone.
  state.mosaics = Object.values(collection.assets || {})
    .filter((a) => (a.roles || []).includes('overview'))
    .map((a) => ({ ...a, href: `${runId}/${a.href.replace(/^\.\//, '')}` }));
  state.bbox = collection.extent?.spatial?.bbox?.[0] ?? null;

  state.run = runId; state.items = items;
  state.selected = null;          // the legend follows the pickers again
  const p = collection.extent?.temporal?.interval?.[0] ?? [];
  $('runmeta').textContent =
    `${items.length} tile${items.length === 1 ? '' : 's'}` +
    (state.mosaics.length ? ' · mosaicked' : '') +
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
 *  map tile is requested and used to look like a hang.
 *
 *  `pending` is NOT zeroed: with a stack, other layers can have map tiles in flight across a
 *  reset, and zeroing would let their completions drive the count negative. It stays exact
 *  instead, because `watchTiles` also counts a tile that is unloaded before it arrives. */
function drawingReset(phase) {
  drawing.drawn = drawing.failed = 0;
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

/** Count one map tile in flight, and re-enter `loading` when a pan or zoom asks for more.
 *
 *  Leaflet aborts a tile that is still loading when it prunes it or the layer is removed, and an
 *  aborted tile fires neither `tileload` nor `tileerror` - only `tileunload`. So each layer keeps
 *  the set it is waiting on, and an unload of one of those settles it. */
function watchTiles(layer) {
  const inflight = new Set();
  const at = (e) => `${e.coords.x}:${e.coords.y}:${e.coords.z}`;
  const settle = (e) => {
    if (!inflight.delete(at(e))) return false;
    drawing.pending--;
    return true;
  };
  layer.on('tileloadstart', (e) => {
    if (!inflight.has(at(e))) { inflight.add(at(e)); drawing.pending++; }
    if (drawing.phase !== 'loading') { clearTimeout(drawing.settle); drawing.phase = 'loading'; }
    drawingTick();
  });
  layer.on('tileload', (e) => { if (settle(e)) drawing.drawn++; drawingTick(); });
  layer.on('tileerror', (e) => { if (settle(e)) drawing.failed++; drawingTick(); });
  layer.on('tileunload', (e) => { if (settle(e)) drawingTick(); });
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

/* Pane z-order. The basemap is Leaflet's `tilePane` (200) and the tile outlines and mask outlines
   are in `overlayPane` (400), so every product layer sits between them: the stack from STACK_BASE
   upward, top row highest, and the preview above the whole stack. */
const STACK_BASE = 210;
const PREVIEW_Z = 390;
let paneSeq = 0;

/** What the pickers' run is, captured - so a pinned layer keeps drawing its own run after the
 *  pickers have moved to another one. */
const snapshotSource = () =>
  ({ run: state.run, items: state.items, mosaics: state.mosaics, bbox: state.bbox });

/** How one asset of one run is to be drawn: the style the tile requests carry, and the legend. */
async function styleFor(src, key, ramp) {
  const first = src.items.find((i) => i.assets?.[key]);
  const asset = first?.assets[key];
  const roles = asset?.roles || [];
  const opts = { ramp };
  let legend = null;
  if (roles.includes('visual')) {
    opts.kind = 'rgba';
  } else {
    legend = await legendFor(first, key);
    if (legend?.kind === 'categorical') {
      Object.assign(opts, { kind: 'categorical', entries: legend.entries,
                            legendHref: first.assets[`${key}_legend`].href,
                            present: presentClasses(src.items, key) });
    } else {
      opts.kind = 'continuous';
      Object.assign(opts, await measure(src.run, key));
    }
  }
  return { asset, opts, legend };
}

/** One overlay for a layer that has a mosaic; otherwise one per item, because one raster per
 *  tile is how the products are written. The per-item fallback is what made a zoomed-out view
 *  slow - every map tile asked every product tile under it - and is kept for runs no one has
 *  mosaicked. */
function buildOverlays(src, key, opts, pane) {
  const mosaics = src.mosaics.filter((m) => m['stratum:asset'] === key);
  const sources = mosaics.length
    ? mosaics.map((m) => ({ href: m.href, bbox: src.bbox }))
    : src.items.filter((i) => i.assets?.[key])
      .map((i) => ({ href: i.assets[key].href, bbox: i.bbox }));
  return sources.map((s) => {
    const bounds = s.bbox
      ? L.latLngBounds([s.bbox[1], s.bbox[0]], [s.bbox[3], s.bbox[2]]) : undefined;
    return watchTiles(rasterLayer(s.href, { ...opts, bounds, pane })).addTo(map);
  });
}

/** A drawn layer: its own pane, its overlays in it, and what the legend needs. */
function makeLayer(src, key, styled, opacity, zIndex) {
  const id = ++paneSeq;
  const paneName = `layer-${id}`;
  const pane = map.createPane(paneName);
  pane.style.zIndex = zIndex;
  pane.style.opacity = opacity;
  return { id, key, run: src.run, title: styled.asset?.title || key,
           description: styled.asset?.description || '', src, opts: styled.opts,
           legend: styled.legend, paneName, pane, visible: true, opacity,
           overlays: buildOverlays(src, key, styled.opts, paneName) };
}

function dropLayer(layer) {
  for (const o of layer.overlays) map.removeLayer(o);
  L.DomUtil.remove(layer.pane);
  delete map.getPanes()[layer.paneName];
}

/** The preview: whatever the pickers point at, drawn above the stack and replaced whenever they
 *  change. A picker change while the legend and range are still being fetched supersedes it, so
 *  only the latest request ever reaches the map. */
async function drawLayer() {
  if (state.preview) { dropLayer(state.preview); state.preview = null; }
  const seq = ++state.drawSeq;
  const key = state.layer;
  $('pin').disabled = true;
  if (!key) { renderLegend(); drawingReset('idle'); return; }
  drawingReset('preparing');

  const src = snapshotSource();
  const styled = await styleFor(src, key, $('ramp').value || 'viridis');
  if (seq !== state.drawSeq) return;
  $('layermeta').textContent = styled.asset?.description || '';
  $('ramprow').hidden = styled.opts.kind !== 'continuous';

  const layer = makeLayer(src, key, styled, $('opacity').value / 100, PREVIEW_Z);
  state.preview = layer;
  $('pin').disabled = false;
  // No overlay means no map tile will ever be requested, so nothing would move it off
  // `preparing`. Say it plainly instead of spinning for ever.
  drawing.phase = layer.overlays.length ? 'loading' : 'settled';
  paintDrawing();       // unconditionally: a coalesced repaint may still be in flight, and the
                        // phase just changed under it
  renderLegend();
}

/* ------------------------------------------------------------------ the stack */

/** Move the preview into the stack, on top. Its pane is reused, so nothing is fetched again. */
function pinPreview() {
  const layer = state.preview;
  if (!layer) return;
  state.preview = null;
  $('pin').disabled = true;
  state.stack.unshift(layer);
  state.selected = layer.id;
  restack();
  if (!stackOpen()) toggleStack(true);
}

function removeLayer(id) {
  const i = state.stack.findIndex((l) => l.id === id);
  if (i < 0) return;
  dropLayer(state.stack[i]);
  state.stack.splice(i, 1);
  if (state.selected === id) state.selected = null;
  restack();
}

function moveLayer(id, to) {
  const from = state.stack.findIndex((l) => l.id === id);
  if (from < 0) return;
  const [layer] = state.stack.splice(from, 1);
  state.stack.splice(Math.max(0, Math.min(to, state.stack.length)), 0, layer);
  restack();
}

/** After any change to the stack: z-order from list order, the mask onto the new top, and the
 *  panel and legend redrawn to match. */
function restack() {
  const n = state.stack.length;
  state.stack.forEach((l, i) => {
    l.pane.style.zIndex = STACK_BASE + (n - 1 - i);
    l.pane.style.display = l.visible ? '' : 'none';
    l.pane.style.opacity = l.opacity;
  });
  applyMask();
  renderStack();
  renderLegend();
}

const stackOpen = () => !$('stackbody').hidden;
function toggleStack(open) {
  $('stackbody').hidden = !open;
  $('stacktoggle').setAttribute('aria-expanded', String(open));
  document.body.classList.toggle('stack-open', open);
}

/** The panel's list, top of the stack first. Rebuilt whole on every change: it is a handful of
 *  rows, and one template is easier to keep right than a set of in-place patches. */
function renderStack() {
  $('stackcount').textContent = state.stack.length;
  $('stackempty').hidden = state.stack.length > 0;
  const last = state.stack.length - 1;
  $('stacklist').innerHTML = state.stack.map((l, i) => `
    <li data-id="${l.id}" class="${l.id === state.selected ? 'selected' : ''}">
      <span class="grip" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
      <input class="vis" type="checkbox" ${l.visible ? 'checked' : ''}
             aria-label="Show ${escapeHTML(l.title)}">
      <button class="name" type="button" aria-pressed="${l.id === state.selected}"
              title="Show this layer's legend">${escapeHTML(l.title)}
        <small>${escapeHTML(l.run)}${i === 0 && state.mask.rings.length && state.mask.peek
          ? ' · peek' : ''}</small></button>
      <button class="up" type="button" aria-label="Move ${escapeHTML(l.title)} up"
              ${i === 0 ? 'disabled' : ''}>↑</button>
      <button class="down" type="button" aria-label="Move ${escapeHTML(l.title)} down"
              ${i === last ? 'disabled' : ''}>↓</button>
      <button class="rm" type="button" aria-label="Remove ${escapeHTML(l.title)}">×</button>
      <input class="op" type="range" min="0" max="100" value="${Math.round(l.opacity * 100)}"
             aria-label="Opacity of ${escapeHTML(l.title)}">
    </li>`).join('');
}

const rowLayer = (el) => {
  const li = el.closest('li[data-id]');
  return li ? state.stack.find((l) => l.id === Number(li.dataset.id)) : null;
};

/* Reordering by drag: pointer events on the grip, not HTML5 drag-and-drop, which a touch screen
   does not reliably start. The drop lands above or below a row by which half the pointer is over. */
const drag = { id: null, target: null, after: false };
function wireStackList() {
  const list = $('stacklist');
  list.addEventListener('click', (e) => {
    const layer = rowLayer(e.target);
    if (!layer) return;
    const i = state.stack.indexOf(layer);
    if (e.target.closest('.up')) moveLayer(layer.id, i - 1);
    else if (e.target.closest('.down')) moveLayer(layer.id, i + 1);
    else if (e.target.closest('.rm')) removeLayer(layer.id);
    else if (e.target.closest('.name')) {
      state.selected = state.selected === layer.id ? null : layer.id;
      renderStack(); renderLegend();
    }
  });
  list.addEventListener('change', (e) => {
    const layer = rowLayer(e.target);
    if (layer && e.target.classList.contains('vis')) {
      layer.visible = e.target.checked;
      layer.pane.style.display = layer.visible ? '' : 'none';
    }
  });
  list.addEventListener('input', (e) => {
    const layer = rowLayer(e.target);
    if (layer && e.target.classList.contains('op')) {
      layer.opacity = e.target.value / 100;
      layer.pane.style.opacity = layer.opacity;
    }
  });
  const clearMarks = () => list.querySelectorAll('li').forEach((li) =>
    li.classList.remove('drop-before', 'drop-after', 'dragging'));
  list.addEventListener('pointerdown', (e) => {
    const grip = e.target.closest('.grip');
    if (!grip || e.button !== 0) return;
    e.preventDefault();
    try { grip.setPointerCapture(e.pointerId); } catch { /* moves still reach the list */ }
    drag.id = Number(grip.closest('li').dataset.id);
    drag.target = null;
    grip.closest('li').classList.add('dragging');
  });
  list.addEventListener('pointermove', (e) => {
    if (drag.id === null) return;
    // Captured, the events target the grip - so ask what is under the pointer instead.
    const li = document.elementFromPoint(e.clientX, e.clientY)?.closest('#stacklist li[data-id]');
    list.querySelectorAll('li').forEach((r) => r.classList.remove('drop-before', 'drop-after'));
    drag.target = li && Number(li.dataset.id) !== drag.id ? Number(li.dataset.id) : null;
    if (drag.target === null) return;
    const r = li.getBoundingClientRect();
    drag.after = e.clientY > r.top + r.height / 2;
    li.classList.add(drag.after ? 'drop-after' : 'drop-before');
  });
  const finish = () => {
    if (drag.id !== null && drag.target !== null) {
      const from = state.stack.findIndex((l) => l.id === drag.id);
      let to = state.stack.findIndex((l) => l.id === drag.target) + (drag.after ? 1 : 0);
      if (from < to) to--;             // the row is taken out before it is put back
      moveLayer(drag.id, to);
    }
    drag.id = drag.target = null;
    clearMarks();
  };
  list.addEventListener('pointerup', finish);
  list.addEventListener('pointercancel', () => { drag.target = null; finish(); });
}

/* ------------------------------------------------------- the peek-through mask

   Shapes drawn on the map cut a window in the TOP layer of the stack: inside them it draws, and
   everywhere else it is transparent, so the layers beneath show through. The shapes belong to
   the top position, not to a layer - reorder, and the window moves to whatever is now on top.

   The cut is a CSS `clip-path` on the top layer's pane. Shapes are kept as lat/lng rings, which
   survive any pan or zoom, and turned into a path in LAYER-POINT pixels, the space a pane's
   contents are positioned in. Panning moves the whole map pane and leaves layer points alone, so
   the path is recomputed only when they change - on zoom and view reset. During the zoom
   animation the tiles are scaled under a path that is about to be wrong, so the masked pane is
   hidden for its duration rather than shown cut in the wrong place.
*/
state.mask = { rings: [], peek: true, outlines: L.layerGroup().addTo(map), masked: null };

/** Twice the signed area of a ring in screen pixels. Its sign is the ring's winding. */
function signedArea(points) {
  let a = 0;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    a += (points[j].x - points[i].x) * (points[j].y + points[i].y);
  }
  return a;
}

/** The rings as one SVG path. Every ring is wound the same way first: under the default
 *  `nonzero` rule two overlapping rings of OPPOSITE winding cancel where they overlap, and a
 *  lasso can be drawn either way round - so without this, overlapping shapes punch holes in each
 *  other instead of adding together. */
function maskPath() {
  return state.mask.rings.map((ring) => {
    let pts = ring.map((ll) => map.latLngToLayerPoint(ll));
    if (signedArea(pts) < 0) pts = pts.reverse();
    return 'M' + pts.map((p) => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' L') + ' Z';
  }).join(' ');
}

function applyMask() {
  for (const l of state.stack) l.pane.style.clipPath = '';
  const top = state.stack[0];
  const on = !!top && state.mask.peek && state.mask.rings.length > 0;
  state.mask.masked = on ? top : null;
  if (on) top.pane.style.clipPath = `path('${maskPath()}')`;
  const n = state.mask.rings.length;
  $('maskmeta').textContent = !n ? ''
    : `${n} shape${n === 1 ? '' : 's'} — ${!top ? 'add a layer for it to cut'
       : on ? `peeking through ${top.title}` : 'peek is off'}`;
}

/** How a shape's edge is drawn, while drawing and after. The colour is in the stylesheet
 *  (`.mask-outline`): white over a dark halo, which reads on imagery and on a street map alike. */
const OUTLINE = { interactive: false, className: 'mask-outline', weight: 2, dashArray: '5 4',
                  fill: false };

function drawOutlines() {
  state.mask.outlines.clearLayers();
  for (const ring of state.mask.rings) L.polygon(ring, OUTLINE).addTo(state.mask.outlines);
}

function commitRing(ring) {
  state.mask.rings.push(ring);
  drawOutlines(); applyMask(); renderStack();
}

map.on('zoomanim', () => {
  if (state.mask.masked) state.mask.masked.pane.style.visibility = 'hidden';
});
map.on('zoomend viewreset', () => {
  applyMask();
  for (const l of state.stack) l.pane.style.visibility = '';
});

/* The draw tools: rectangle and freehand lasso. While one is armed the map does not pan and a
   click does not read a point - a drag draws, and Esc disarms. It stays armed after a shape, so
   several can be drawn in a row. Pointer events, so a finger draws as well as a mouse. */
const draw = { live: null, start: null, points: null, lastPx: null };

function setTool(tool) {
  cancelDraw();
  state.tool = state.tool === tool ? null : tool;
  for (const [id, t] of [['toolrect', 'rect'], ['toollasso', 'lasso']]) {
    $(id).setAttribute('aria-pressed', String(state.tool === t));
  }
  document.body.classList.toggle('tool-active', !!state.tool);
  if (state.tool) { map.dragging.disable(); map.doubleClickZoom.disable(); map.boxZoom.disable(); }
  else { map.dragging.enable(); map.doubleClickZoom.enable(); map.boxZoom.enable(); }
}

function cancelDraw() {
  if (draw.live) map.removeLayer(draw.live);
  draw.live = draw.start = draw.points = draw.lastPx = null;
}

const container = map.getContainer();
container.addEventListener('pointerdown', (e) => {
  if (!state.tool || e.button !== 0 || e.target.closest('.leaflet-control')) return;
  e.preventDefault();
  // Capture keeps the drag ours when the pointer leaves the map; it is a nicety, and throws for
  // a pointer the browser no longer considers active, so a failure must not lose the shape.
  try { container.setPointerCapture(e.pointerId); } catch { /* drawn without capture */ }
  const ll = map.mouseEventToLatLng(e);
  draw.start = ll;
  draw.lastPx = map.mouseEventToContainerPoint(e);
  if (state.tool === 'rect') {
    draw.live = L.rectangle(L.latLngBounds(ll, ll), OUTLINE).addTo(map);
  } else {
    draw.points = [ll];
    draw.live = L.polyline(draw.points, OUTLINE).addTo(map);
  }
});
container.addEventListener('pointermove', (e) => {
  if (!draw.live) return;
  const ll = map.mouseEventToLatLng(e);
  if (state.tool === 'rect') {
    draw.live.setBounds(L.latLngBounds(draw.start, ll));
    return;
  }
  const px = map.mouseEventToContainerPoint(e);
  if (px.distanceTo(draw.lastPx) < 3) return;      // a point per 3 px is plenty for a lasso
  draw.lastPx = px;
  draw.points.push(ll);
  draw.live.addLatLng(ll);
});
container.addEventListener('pointerup', (e) => {
  if (!draw.live) return;
  let ring = null;
  if (state.tool === 'rect') {
    const b = draw.live.getBounds();
    const size = map.latLngToContainerPoint(b.getNorthEast())
      .subtract(map.latLngToContainerPoint(b.getSouthWest()));
    if (Math.abs(size.x) >= 4 && Math.abs(size.y) >= 4) {      // a click is not a rectangle
      ring = [b.getSouthWest(), b.getNorthWest(), b.getNorthEast(), b.getSouthEast()];
    }
  } else if (draw.points.length >= 3) {
    ring = draw.points.slice();
  }
  cancelDraw();
  try { container.releasePointerCapture(e.pointerId); } catch { /* never captured */ }
  if (ring) commitRing(ring);
});
container.addEventListener('pointercancel', cancelDraw);
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !state.tool) return;
  if (draw.live) cancelDraw(); else setTool(state.tool);
});

/** A continuous layer's range, measured once over the run's tiles and used for all of them. The
 *  server caches it per layer, so this costs nothing after the first time a layer is drawn.
 *  Falling back to 0..1 keeps a layer drawing rather than vanishing when the measurement fails -
 *  still one range for every tile, so still no tile edges. */
async function measure(run, layer) {
  try {
    const query = new URLSearchParams({ run, layer });
    return await fetchJSON(`api/range?${query}`);
  } catch {
    return { vmin: 0, vmax: 1 };
  }
}

/** The class ids a layer actually holds anywhere in the run, or `null` when the run cannot say.
 *
 *  Publish writes a per-tile `count` on every `classification:classes` entry, so this is a union
 *  over the items already loaded - no raster is read. A run published before counts existed has
 *  none, and then the whole table is shown rather than an empty legend. */
function presentClasses(items, key) {
  const present = new Set();
  let counted = false;
  for (const item of items) {
    for (const c of classesOf(item, key) || []) {
      if (c.count === undefined) continue;
      counted = true;
      if (c.count > 0) present.add(c.value);
    }
  }
  return counted ? present : null;
}

/** The legend of the stack row that is selected, else of the preview. */
function renderLegend() {
  const el = $('legend');
  const layer = state.stack.find((l) => l.id === state.selected) || state.preview;
  if (!layer) { el.innerHTML = ''; return; }
  const { opts, legend, key } = layer;
  const from = layer === state.preview ? ''
    : `<p class="muted">Pinned layer · <code>${escapeHTML(layer.run)}</code></p>`;
  if (opts.kind === 'categorical' && legend) {
    const all = legend.entries.filter((e) => e.id !== 0);
    const rows = opts.present ? all.filter((e) => opts.present.has(e.id)) : all;
    const scope = opts.present
      ? `Only the ${rows.length} of ${all.length} classes that occur in this run.`
      : 'Every class in the table - this run was published without per-class counts.';
    el.innerHTML = `<h2>${escapeHTML(key)} — ${rows.length} classes</h2>` + from +
      '<input id="legendfilter" type="search" placeholder="filter classes" ' +
      'aria-label="Filter classes">' +
      `<p class="muted">${scope} Or click the map: it reads every band at that point.</p><ul>` +
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
    el.innerHTML = `<h2>${escapeHTML(key)}</h2>` + from +
      (stops ? `<div class="bar" style="background:linear-gradient(to right,${stops})"></div>` : '') +
      `<div class="ticks"><span>${fmt(lo)}</span><span>${fmt(hi)}</span></div>` +
      '<p class="muted">Stretched to the 2nd–98th percentile of the layer across the run — ' +
      'one scale for every tile.</p>';
  } else {
    el.innerHTML = `<h2>${escapeHTML(key)}</h2>` + from + `<p class="muted">A published rendering: its colours ` +
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
  map.fitBounds(bounds, { paddingTopLeft: [stackOpen() ? 300 : 10, 10],
                          paddingBottomRight: [330, 10] });
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

map.on('click', (e) => { if (!state.tool) readPoint(e.latlng); });  // armed, a drag draws

$('run').addEventListener('change', async (e) => {
  await loadRun(e.target.value); drawGrid(); fitRun();
});
$('layer').addEventListener('change', (e) => { state.layer = e.target.value; drawLayer(); });
$('ramp').addEventListener('change', drawLayer);
$('tiles').addEventListener('change', drawGrid);
$('basemap').addEventListener('change', (e) => setBasemap(e.target.value));
$('opacity').addEventListener('input', (e) => {
  $('opacityval').textContent = `${e.target.value}%`;
  if (state.preview) state.preview.pane.style.opacity = state.preview.opacity = e.target.value / 100;
});
$('pin').addEventListener('click', pinPreview);
$('stacktoggle').addEventListener('click', () => toggleStack(!stackOpen()));
$('toolrect').addEventListener('click', () => setTool('rect'));
$('toollasso').addEventListener('click', () => setTool('lasso'));
$('maskclear').addEventListener('click', () => {
  state.mask.rings = [];
  drawOutlines(); applyMask(); renderStack();
});
$('peek').addEventListener('change', (e) => {
  state.mask.peek = e.target.checked;
  // Peek off keeps the shapes and their outlines; it only stops the cut.
  applyMask(); renderStack();
});
wireStackList();
renderStack();

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
