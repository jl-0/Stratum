"""The resolve stage for one work item: streaming argmax over a block (04 section 4, 12
section 2, 06 section 2).

    item = {"tile": [tx, ty], "epoch": [start_iso, end_iso], "block": [bx, by]}

The loop keeps a running best score plus the layers of the current best, so memory is
O(block x n_layers) whatever the observation count. Cells are taken by strict `>`: on a tie the
observation seen first - the earliest by acquisition time - keeps the cell, which is what
makes the result independent of how many blocks the tile is cut into and is the streaming
counterpart of `tie_break: earliest` (11 section 5).

A `stack` scorer (04 section 4) takes the other path, `_resolve_stack`: the same reads, one
granule at a time, but each observation's block window is kept, `score` is called once with all
N rows, and the argmax runs along the observation axis with the same tie rule. Memory is then
O(N x block x kept bytes), and `block_size` is the knob that bounds it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from rasterio.warp import transform_bounds

from stratum.ancillary import BlockAux, aux_key_for
from stratum.cache import CacheKey, glt_inputs, snapshot_inputs
from stratum.classes import Remap
from stratum.index import role_asset
from stratum.regrid import read_glt
from stratum.resolve.context import NullAux, PlanContext, scorer_capability
from stratum.resolve.observation import (
    ObsContext,
    Observation,
    block_coords,
    is_lonlat,
    read_observation,
)
from stratum.resolve.snapshot import empty_layer, write_snapshot
from stratum.types import (
    AuxAccessor,
    BlockRef,
    Coords,
    Epoch,
    GranuleRef,
    ObsWindow,
    TileRef,
    canonical_hash,
    scorer_output,
)


# ------------------------------------------------------------------------------------ work item
@dataclass(frozen=True)
class ResolveItem:
    """A parsed resolve work item. `block` carries the scorer's halo."""

    tile: TileRef
    epoch: Epoch
    block: BlockRef

    @classmethod
    def parse(cls, item: Mapping[str, Any], plan: PlanContext) -> ResolveItem:
        tx, ty = (int(v) for v in item["tile"])
        bx, by = (int(v) for v in item["block"])
        start, end = (_utc(datetime.fromisoformat(str(s))) for s in item["epoch"])
        tile = TileRef(plan.grid, tx, ty)
        halo = int(getattr(plan.scorer.instance, "halo", 0) or 0)
        if halo < 0:
            raise ValueError(f"scorer halo must be >= 0; got {halo}")
        return cls(tile=tile, epoch=Epoch(start, end), block=BlockRef(tile, bx, by, halo=halo))


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


# ----------------------------------------------------------------------------------- candidates
def window_lonlat_bounds(block: BlockRef) -> tuple[float, float, float, float]:
    """(w, s, e, n) in EPSG:4326 of the block's halo window, for the index bbox test."""
    w = block.window
    t = block.transform
    x0, y0 = t.c, t.f
    x1, y1 = t.c + w.width * t.a, t.f + w.height * t.e
    west, east = min(x0, x1), max(x0, x1)
    south, north = min(y0, y1), max(y0, y1)
    crs = block.tile.grid.crs
    if is_lonlat(crs):
        return (west, south, east, north)
    return transform_bounds(crs, "EPSG:4326", west, south, east, north)


def candidates(plan: PlanContext, block: BlockRef, epoch: Epoch) -> list[GranuleRef]:
    """Step 2 of 12 section 2: granules whose bbox meets the block window and whose acquisition
    time lies in the epoch, ordered by (datetime, granule_id) so the loop - and therefore any
    tie - is deterministic."""
    w, s, e, n = window_lonlat_bounds(block)
    out = []
    for ref in plan.granules.values():
        gw, gs, ge, gn = ref.bbox
        if gw > e or ge < w or gs > n or gn < s:
            continue
        if not epoch.contains(_utc(ref.datetime)):
            continue
        out.append(ref)
    out.sort(key=lambda r: (_utc(r.datetime), r.granule_id))
    return out


def glt_key_for(plan: PlanContext, tile: TileRef, granule_id: str) -> CacheKey:
    """The key regrid wrote the granule's GLT under: the same inputs, so the same hash."""
    inputs = glt_inputs(granule_id, plan.grid, plan.max_distance, plan.regrid_method,
                        plan.regrid_algo_version, pad=plan.tile_pad)
    return plan.cache.key("glt", plan.grid.id, tile, inputs)


def aux_keys_for(plan: PlanContext, tile: TileRef) -> dict[str, CacheKey]:
    """alias -> the warp key for every aux source a plugin declared (06 section 2).

    PURE: no IO, no cache probe, no network. That is not tidiness. `snapshot_key` is recomputed
    by `product_key` once per epoch per block through the whole reduce stage, and again at
    publish, so a stat or an open in here turns reduce into an IO storm of O(blocks x epochs).

    Keyed on the DECLARATION (`PlanContext.aux_to_read`), not on what a plugin goes on to read:
    the snapshot key is built before the scorer runs, so what was read is not knowable yet. A
    declared-but-unread source therefore costs a spurious miss - the safe direction, and the only
    computable rule. Per tile, not per block: a warp is shared by every block of the tile
    (05 section 4).
    """
    return {alias: aux_key_for(plan.cache, tile, plan.aux_sources[alias], plan.tile_pad)
            for alias in plan.aux_to_read()}


def granule_remap(plan: PlanContext, layer: str, granule_id: str) -> Remap:
    """The raw -> product lookup the planner resolved for one granule on one categorical layer
    (13 section 3 rule 2); a missing one is a plan defect, not a resolve decision."""
    try:
        return plan.remaps[layer][granule_id]
    except KeyError:
        raise KeyError(f"no class remap for granule {granule_id!r} on layer {layer!r}; "
                       "per-granule resolution is a plan-time step (13 section 3 rule 2)") from None


def observation_inputs(plan: PlanContext, glt_key: CacheKey, granule: GranuleRef,
                       aux_keys: Mapping[str, CacheKey] | None = None) -> dict[str, Any]:
    """The masked-observation identity of 06 section 2 for ONE granule: `glt_key`,
    `asset_roles` (which collection/asset/variable each role reads - the bindings, not URIs,
    which vary between environments - plus that asset's catalogue checksum, the asset identity
    of 12 section 4; None when the source has none), `pixel_mask_spec` and
    `mask_plugin_version`, and `remaps`: for every categorical layer the raw class table's
    fingerprint and a hash of the resolved lookup, because the remap is applied at the gather
    (13 section 3 rule 3) and so determines the observation as much as a mask does. Nothing is
    written under this key in this slice, but its hash is what enters the snapshot key as an
    `obs_key`, so a mask, a lumping or a re-delivered table invalidates snapshots and not
    GLTs (06 section 3 rule 1).

    A MAP-space mask that reads an aux raster is determined by that raster too, and neither
    `pixel_mask_spec` nor `mask_plugin_version` captures it - swap the raster and the mask would
    silently admit different pixels. `mask_aux_keys` closes that, and is emitted only when a mask
    actually declares aux, so a run without one produces exactly the keys it produced before aux
    existed (the same trick `glt_inputs` uses for `source_checksum`)."""
    read = plan.roles_to_read()
    asset_roles: dict[str, Any] = {}
    for role, b in plan.roles.items():
        if role not in read:
            continue
        key = role_asset(granule, b)
        asset_roles[role] = {"collection": b.collection, "asset": b.asset, "var": b.var,
                             "checksum": granule.checksums.get(key) if key else None}
    remaps: dict[str, Any] = {}
    for layer in plan.schema.layers:
        if layer.kind != "categorical":
            continue
        remap = granule_remap(plan, layer.name, granule.granule_id)
        remaps[layer.name] = {"raw_fingerprint": remap.raw_fingerprint,
                              "enumeration": remap.enumeration,
                              "lookup": canonical_hash(remap.lookup.tolist())}
    inputs: dict[str, Any] = {
        "artifact_type": "observation",
        "glt_key": glt_key.hash,
        "asset_roles": asset_roles,
        "pixel_mask_spec": [{"ref": m.ref, "params": dict(m.params)} for m in plan.masks],
        "mask_plugin_version": [m.version for m in plan.masks],
        "remaps": remaps,
    }
    mask_aux = plan.map_mask_aux()
    if mask_aux:
        if aux_keys is None:
            raise ValueError(
                "a map-space mask declares required_aux, so the observation key needs the aux "
                "warp keys; pass aux_keys= (a tile is not recoverable from a GLT key)")
        inputs["mask_aux_keys"] = {a: aux_keys[a].hash for a in mask_aux}
    return inputs


# ------------------------------------------------------------------------------------- the loop
@dataclass
class Resolved:
    """The streaming argmax over one block, trimmed to the core window."""

    layers: dict[str, np.ndarray]
    score: np.ndarray
    valid: np.ndarray
    contributing: list[str]          # granule ids that won at least one core cell
    observed: np.ndarray | None = None   # any GLT hit, before masks; None = not tracked


def resolve_window(plan: PlanContext, block: BlockRef, epoch: Epoch,
                   reached: list[tuple[GranuleRef, CacheKey, np.ndarray]],
                   aux: AuxAccessor | None = None) -> Resolved:
    """Score every observation that reaches the block and keep the best per cell (04 section 4,
    streaming). `reached` is [(granule, glt_key, glt_window)] in candidate order. A cell is
    taken by observation i when valid, its score is not NaN, and it is strictly greater than
    the running best (or nothing has won yet): first wins a tie."""
    win = block.window
    shape = (win.height, win.width)
    best = np.full(shape, np.nan, dtype=np.float32)
    won = np.zeros(shape, dtype=bool)
    # The footprint, before any pixel mask or the scorer: every cell a granule's GLT reaches. It
    # costs nothing - the GLT windows are already in hand - and it is the only record of where
    # EMIT looked, as opposed to where a look survived.
    observed = np.zeros(shape, dtype=bool)
    for _, _, glt in reached:
        observed |= np.asarray(glt)[..., 2] != 0
    layers: dict[str, np.ndarray] = {}
    contributing: list[str] = []
    if aux is None:
        aux = NullAux() if not plan.aux_to_read() else BlockAux(
            aux_keys_for(plan, block.tile), plan.aux_sources, block, plan.cache)
    scorer = plan.scorer.instance
    core = _core_slices(block)
    coords = block_coords(block.transform, shape, plan.grid.crs)
    if scorer_capability(scorer) == "stack":
        return _resolve_stack(plan, block, epoch, reached, aux, coords, observed)
    emits = bool(plan.scorer_layers())
    for granule, key, glt in reached:
        result = read_observation(ObsContext(plan, block, epoch, granule, key, glt, aux, coords))
        if result is None:
            continue
        obs = result.obs
        score = np.asarray(np.ma.filled(scorer.score(obs, aux), np.nan), dtype=np.float32)
        if score.shape != shape:
            raise ValueError(f"scorer {plan.scorer.ref!r} returned {score.shape}; expected "
                             f"{shape} (04 section 4)")
        emitted = (_check_emit(plan, scorer.emit(obs, aux), n=None, shape=shape)
                   if emits else {})
        take = obs.valid & ~np.isnan(score) & (~won | (score > best))
        if not take.any():
            continue
        best[take] = score[take]
        won |= take
        for layer in plan.schema.layers:
            output = scorer_output(layer.source)
            values = result.layers[layer.name] if output is None else emitted[output]
            if layer.name not in layers:
                bands = values.shape[2] if values.ndim == 3 else 1
                layers[layer.name] = empty_layer(layer, shape, bands)
            layers[layer.name][take] = values[take].astype(layers[layer.name].dtype)
        if take[core].any():
            contributing.append(granule.granule_id)
    for layer in plan.schema.layers:
        if layer.name not in layers:
            # No observation won this layer anywhere in the block. It still needs a plane, and
            # the plane still needs the layer's BAND COUNT - a declared subset knows it without
            # reading anything, and getting it wrong writes an (H, W) plane for a layer whose
            # other epochs are (H, W, B), which `stack_snapshots` then cannot stack.
            layers[layer.name] = empty_layer(layer, shape, len(layer.bands) if layer.bands else 1)
    return Resolved(layers={k: v[core] for k, v in layers.items()}, score=best[core],
                    valid=won[core], contributing=contributing, observed=observed[core])


# -------------------------------------------------------------------------------- the stack mode
class _StackBuffers:
    """(N, H, W[, B]) buffers for a `stack` resolve, allocated on the first observation and
    filled row by row as each granule is read (04 section 4).

    Only block windows are kept, and of each only what the scorer may read and what the gather
    writes: the roles the scorer names (whole), the aliases it names whose role is not kept (that
    one band, copied), every schema layer's value, `valid` and `interpolated`. A layer whose
    value IS a kept band (`PlanContext.stack_shared_layers`) gets no buffer of its own: the
    window serves it from the band's. Each observation's own arrays are dropped as soon as its
    row is written, so no granule is read twice and nothing is stacked by a second copy."""

    def __init__(self, n_max: int, shared: Mapping[str, str] | None = None) -> None:
        self.n_max = n_max
        self.shared = dict(shared or {})     # layer name -> kept band (role or alias) name
        self.n = 0
        self.granules: list[GranuleRef] = []
        self.bands: dict[str, np.ndarray] = {}
        self.layers: dict[str, np.ndarray] = {}
        self.valid: np.ndarray | None = None
        self.interpolated: np.ndarray | None = None
        self.band_attrs: dict[str, Any] = {}

    @staticmethod
    def _put(store: dict[str, np.ndarray], name: str, row: int, n_max: int,
             value: np.ndarray) -> None:
        """Write `value` as row `row` of `store[name]`. The buffer starts at the first row's
        dtype and is PROMOTED (np.result_type, value-preserving) when a later row brings a wider
        one - a granule of another product version can carry int16 where an earlier one had
        float32 - so no row is ever truncated by the buffer. The gather casts the winning value
        to the layer's dtype exactly as streaming casts each observation's own."""
        value = np.asarray(value)
        buf = store.get(name)
        if buf is None:
            buf = store[name] = np.empty((n_max, *value.shape), dtype=value.dtype)
        elif value.dtype != buf.dtype:
            wider = np.result_type(buf.dtype, value.dtype)
            if wider != buf.dtype:
                grown = np.empty(buf.shape, dtype=wider)
                grown[:row] = buf[:row]
                buf = store[name] = grown
        buf[row] = value

    def add(self, result: Observation, keep_roles: list[str],
            copy_aliases: dict[str, str]) -> None:
        obs, row = result.obs, self.n
        if row >= self.n_max:
            raise IndexError(f"more observations than the {self.n_max} reached")
        for name in (*keep_roles, *copy_aliases):
            self._put(self.bands, name, row, self.n_max, obs[name])
        for name, value in result.layers.items():
            if name not in self.shared:
                self._put(self.layers, name, row, self.n_max, value)
        if self.valid is None:
            shape = np.shape(obs.valid)
            self.valid = np.zeros((self.n_max, *shape), dtype=bool)
            self.interpolated = np.zeros((self.n_max, *shape), dtype=bool)
            self.band_attrs = dict(obs.band_attrs)      # per collection, not per granule
        assert self.interpolated is not None
        self.valid[row] = obs.valid
        self.interpolated[row] = obs.interpolated
        self.granules.append(obs.granules[0])
        self.n += 1

    def window(self, plan: PlanContext, block: BlockRef, epoch: Epoch,
               coords: Coords) -> ObsWindow:
        """The stacked ObsWindow over the rows filled. Every kept buffer is made read-only first:
        one buffer backs `obs[...]`, every `at(i)` view and the gather, so a scorer writing into
        it would silently change what the snapshot records."""
        assert self.valid is not None and self.interpolated is not None
        k = self.n
        bands = {name: buf[:k] for name, buf in self.bands.items()}
        layers = {name: buf[:k] for name, buf in self.layers.items()}
        valid, interpolated = self.valid[:k], self.interpolated[:k]
        for arr in (*bands.values(), *layers.values(), valid, interpolated):
            arr.flags.writeable = False
        for name, alias in plan.aliases.items():
            if name not in bands and alias.role in bands:
                bands[name] = bands[alias.role][..., alias.band]
        for name, source in self.shared.items():
            layers[name] = bands[source]
        return ObsWindow(space="block", bands=bands, valid=valid, granules=self.granules,
                         epoch=epoch, block=block, interpolated=interpolated, coords=coords,
                         band_attrs=self.band_attrs, layers=layers, stacked=True,
                         layer_classes=plan.layer_classes())


def stack_argmax(score: np.ndarray, valid: np.ndarray
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The winner per cell of an (N, H, W) score: `(idx, won, best)`.

    A row is a candidate where it is valid and its score is not NaN. Among the candidates the
    highest score wins and equal maxima go to the LOWEST index - the earliest look - which is
    what streaming's strict `>` yields. The maximum is found first and matched second, rather
    than taking a bare argmax over a -inf fill, so a best of -inf still picks a candidate row
    and never an invalid row 0. `idx` is (H, W) int, `won` (H, W) bool, `best` (H, W) float32
    with NaN where nothing won."""
    cand = np.asarray(valid, dtype=bool) & ~np.isnan(score)
    won = cand.any(axis=0)
    top = np.where(cand, score, -np.inf).max(axis=0)
    idx = np.argmax(cand & (score == top[None]), axis=0)
    best = np.where(won, top, np.nan).astype(np.float32)
    return idx, won, best


def _resolve_stack(plan: PlanContext, block: BlockRef, epoch: Epoch,
                   reached: list[tuple[GranuleRef, CacheKey, np.ndarray]],
                   aux: AuxAccessor, coords: Coords, observed: np.ndarray) -> Resolved:
    """`resolve_window` for a `stack` scorer (04 section 4): read each observation exactly as
    streaming does, keep its block window, call `score` once with all N, take the argmax along
    the observation axis and gather every schema layer from the winning row."""
    win = block.window
    shape = (win.height, win.width)
    scorer = plan.scorer.instance
    core = _core_slices(block)
    keep_roles, copy_aliases = plan.stack_keep()
    bufs = _StackBuffers(len(reached), plan.stack_shared_layers())
    for granule, key, glt in reached:
        result = read_observation(ObsContext(plan, block, epoch, granule, key, glt, aux, coords))
        if result is None:
            continue
        bufs.add(result, keep_roles, copy_aliases)
        del result
    layers: dict[str, np.ndarray] = {}
    if bufs.n == 0:
        # nothing reached: the scorer never sees an empty stack, and the snapshot is the one
        # streaming writes for the same block
        for layer in plan.schema.layers:
            layers[layer.name] = empty_layer(layer, shape, len(layer.bands) if layer.bands else 1)
        nothing = np.zeros(shape, dtype=bool)
        return Resolved(layers={k: v[core] for k, v in layers.items()},
                        score=np.full(shape, np.nan, dtype=np.float32)[core],
                        valid=nothing[core], contributing=[], observed=observed[core])
    obs = bufs.window(plan, block, epoch, coords)
    n = obs.n
    score = np.asarray(np.ma.filled(scorer.score(obs, aux), np.nan), dtype=np.float32)
    if score.shape != (n, *shape):
        raise ValueError(f"scorer {plan.scorer.ref!r} (capability 'stack') returned "
                         f"{score.shape}; expected (N, H, W) = {(n, *shape)}, one row per "
                         "observation in obs.granules order (04 section 4)")
    emitted = (_check_emit(plan, scorer.emit(obs, aux), n=n, shape=shape)
               if plan.scorer_layers() else {})
    idx, won, best = stack_argmax(score, obs.valid)
    for layer in plan.schema.layers:
        if not won.any():
            layers[layer.name] = empty_layer(layer, shape, len(layer.bands) if layer.bands else 1)
            continue
        output = scorer_output(layer.source)
        if output is not None and emitted[output].ndim == 2:
            # a per-cell value (a whole-stack property): taken as-is where something won
            out = empty_layer(layer, shape, 1)
            out[won] = emitted[output][won].astype(out.dtype)
            layers[layer.name] = out
            continue
        values = obs.layer(layer.name) if output is None else emitted[output]
        bands = values.shape[3] if values.ndim == 4 else 1
        pick = idx.reshape((1, *shape) + (1,) * (values.ndim - 3))
        chosen = np.take_along_axis(values, pick, axis=0)[0]
        out = empty_layer(layer, shape, bands)
        out[won] = chosen[won].astype(out.dtype)
        layers[layer.name] = out
    won_core, idx_core = won[core], idx[core]
    contributing = [g.granule_id for i, g in enumerate(obs.granules)
                    if (won_core & (idx_core == i)).any()]
    return Resolved(layers={k: v[core] for k, v in layers.items()}, score=best[core],
                    valid=won[core], contributing=contributing, observed=observed[core])


def _check_emit(plan: PlanContext, emitted: Any, *, n: int | None,
                shape: tuple[int, int]) -> dict[str, np.ndarray]:
    """What a scorer's `emit()` returned, checked (04 section 4): a mapping with exactly the
    declared output names, each a numeric array of shape (H, W) when `n` is None (streaming,
    one observation), or (n, H, W) - per observation - or (H, W) - per cell - when stacked.
    Masked values become NaN."""
    ref = plan.scorer.ref
    want = {b.name for b in getattr(plan.scorer.instance, "outputs", ()) or ()}
    got = set(emitted) if isinstance(emitted, Mapping) else set()
    if not isinstance(emitted, Mapping) or got != want:
        raise ValueError(f"scorer {ref!r}: emit() returned {sorted(got)}; it must return exactly "
                         f"its declared outputs {sorted(want)} (04 section 4)")
    out: dict[str, np.ndarray] = {}
    for name in sorted(want):
        value = emitted[name]
        if isinstance(value, np.ma.MaskedArray):
            value = np.ma.filled(value.astype(np.float64), np.nan)
        arr = np.asarray(value)
        if arr.dtype.kind not in "biuf":
            raise ValueError(f"scorer {ref!r}: emit() output {name!r} has dtype {arr.dtype}; "
                             "expected a numeric array (04 section 4)")
        if n is None:
            if arr.shape != shape:
                raise ValueError(f"scorer {ref!r}: emit() output {name!r} has shape "
                                 f"{arr.shape}; expected (H, W) = {shape} (04 section 4)")
        elif arr.shape not in ((n, *shape), shape):
            raise ValueError(f"scorer {ref!r}: emit() output {name!r} has shape {arr.shape}; "
                             f"expected (N, H, W) = {(n, *shape)} per observation or (H, W) = "
                             f"{shape} per cell (04 section 4)")
        out[name] = arr
    return out


def _core_slices(block: BlockRef) -> tuple[slice, slice]:
    h = block.halo
    core = block.core_window
    return (slice(h, h + core.height), slice(h, h + core.width))


# ------------------------------------------------------------------------------------ the stage
def resolve_block(item: Mapping[str, Any], plan: PlanContext) -> CacheKey:
    """One resolve work item: the epoch snapshot for one block (12 section 2, 06 section 2).

    1. Candidates by bbox and epoch, ordered by time.
    2. For each: the GLT window from the cache (a range read); a missing GLT means regrid did
       not run and is an error; no hit in the window means the granule is skipped.
    3. The snapshot key from the observations that reach the block - their masked-observation
       hashes, sorted - the scorer's identity, the schema's `layers_hash` and the epoch bounds;
       beside them, for the sidecar, the raw class-table fingerprints those observations were
       remapped from (13 section 6). Return at once on a hit: nothing else is read.
    4. Otherwise the streaming loop, then the directory written whole and renamed.
    """
    parsed = ResolveItem.parse(item, plan)
    tile, epoch, block = parsed.tile, parsed.epoch, parsed.block
    reached = reached_observations(plan, parsed)
    snap_key = _snapshot_key(plan, parsed, reached)
    if plan.cache.hit(snap_key):
        return snap_key

    aux_keys = aux_keys_for(plan, tile)
    aux = NullAux() if not aux_keys else BlockAux(aux_keys, plan.aux_sources, block, plan.cache)
    resolved = resolve_window(plan, block, epoch, reached, aux)
    core_transform = BlockRef(tile, block.bx, block.by, halo=0).transform
    plan.cache.write_dir(snap_key, lambda d: write_snapshot(
        d, layers=resolved.layers, score=resolved.score, valid=resolved.valid,
        observed=resolved.observed,
        schema=plan.schema, transform=core_transform, crs=plan.grid.crs))
    return snap_key


def reached_observations(plan: PlanContext, parsed: ResolveItem
                         ) -> list[tuple[GranuleRef, CacheKey, np.ndarray]]:
    """Steps 1-2 of `resolve_block`: the candidates whose GLT has a hit in the block window, as
    `(granule, glt_key, glt_window)` in candidate order. A missing GLT is an error: regrid did
    not run (03 section 6)."""
    tile, epoch, block = parsed.tile, parsed.epoch, parsed.block
    reached: list[tuple[GranuleRef, CacheKey, np.ndarray]] = []
    for granule in candidates(plan, block, epoch):
        key = glt_key_for(plan, tile, granule.granule_id)
        if not plan.cache.hit(key):
            raise FileNotFoundError(
                f"regrid did not run for granule {granule.granule_id!r} over tile "
                f"{tile.name}: no GLT at {key.path} (03 section 6; run the regrid stage first)")
        glt = read_glt(key.path, block.window)
        if not (glt[..., 2] != 0).any():
            continue
        reached.append((granule, key, glt))
    return reached


def _snapshot_key(plan: PlanContext, parsed: ResolveItem,
                  reached: list[tuple[GranuleRef, CacheKey, np.ndarray]]) -> CacheKey:
    aux_keys = aux_keys_for(plan, parsed.tile)
    obs_keys = sorted(canonical_hash(observation_inputs(plan, key, granule, aux_keys))
                      for granule, key, _ in reached)
    # sorted by alias, so plugin declaration order cannot change a key (06 section 2)
    inputs = snapshot_inputs(obs_keys, [aux_keys[a].hash for a in sorted(aux_keys)],
                             plan.scorer.ref, plan.scorer.version,
                             plan.scorer.params, plan.schema.layers_hash,
                             list(parsed.epoch.bounds))
    # 13 section 6: every contributing raw table's fingerprint is in the snapshot's
    # .inputs.json. It is already inside each obs_key; listing it here is what lets
    # `stratum cache diff` name a vintage change rather than an opaque obs_key.
    inputs["class_tables"] = {
        layer.name: sorted({granule_remap(plan, layer.name, g.granule_id).raw_fingerprint
                            for g, _, _ in reached})
        for layer in plan.schema.layers if layer.kind == "categorical"}
    return plan.cache.key("snapshot", plan.grid.id, parsed.tile,
                          _with_block(inputs, parsed.block))


def snapshot_key(item: Mapping[str, Any], plan: PlanContext) -> CacheKey:
    """The key `resolve_block` writes (or would write) the item's snapshot under, computed the
    same way - candidates, GLT window hit test, masked-observation hashes - without resolving.
    The reduce stage recomputes its inputs' keys through this so a missing snapshot is found
    before anything is read (06 section 2)."""
    parsed = ResolveItem.parse(item, plan)
    return _snapshot_key(plan, parsed, reached_observations(plan, parsed))


def _with_block(inputs: dict[str, Any], block: BlockRef) -> dict[str, Any]:
    """A snapshot is per tile x epoch x block (06 section 2). The tile is in the key's path;
    the block is not, and two blocks of one tile can be reached by the same granules (or by
    none) while holding different cells, so the block's CORE window on the tile - offsets and
    size, in cells - enters the key beside the 06 fields. The index (bx, by) alone would not do:
    block sizes differ between runs and (0, 0) names a different rectangle under each. The halo
    is excluded because it never changes output (01 section 3 invariant)."""
    core = block.core_window
    return {**inputs, "window": [core.row_off, core.col_off, core.height, core.width]}


__all__ = [
    "ResolveItem", "Resolved", "candidates", "glt_key_for", "granule_remap",
    "observation_inputs", "reached_observations", "resolve_block", "resolve_window",
    "snapshot_key", "stack_argmax", "window_lonlat_bounds",
]
