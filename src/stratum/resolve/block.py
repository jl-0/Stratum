"""The resolve stage for one work item: streaming argmax over a block (04 section 4, 12
section 2, 06 section 2).

    item = {"tile": [tx, ty], "epoch": [start_iso, end_iso], "block": [bx, by]}

The loop keeps a running best score plus the layers of the current best, so memory is
O(block x n_layers) whatever the observation count. Cells are taken by strict `>`: on a tie the
observation seen first - the earliest by acquisition time - keeps the cell, which is what
makes the result independent of how many blocks the tile is cut into and is the streaming
counterpart of `tie_break: earliest` (11 section 5).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from rasterio.warp import transform_bounds

from stratum.cache import CacheKey, glt_inputs, snapshot_inputs
from stratum.classes import Remap
from stratum.index import role_asset
from stratum.regrid import read_glt
from stratum.resolve.context import NullAux, PlanContext
from stratum.resolve.observation import ObsContext, block_coords, is_lonlat, read_observation
from stratum.resolve.snapshot import empty_layer, write_snapshot
from stratum.types import BlockRef, Epoch, GranuleRef, TileRef, canonical_hash


# ------------------------------------------------------------------------------------ work item
@dataclass(frozen=True)
class ResolveItem:
    """A parsed resolve work item (plan section 4). `block` carries the scorer's halo."""

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
                        plan.regrid_algo_version)
    return plan.cache.key("glt", plan.grid.id, tile, inputs)


def granule_remap(plan: PlanContext, layer: str, granule_id: str) -> Remap:
    """The raw -> product lookup the planner resolved for one granule on one categorical layer
    (13 section 3 rule 2); a missing one is a plan defect, not a resolve decision."""
    try:
        return plan.remaps[layer][granule_id]
    except KeyError:
        raise KeyError(f"no class remap for granule {granule_id!r} on layer {layer!r}; "
                       "per-granule resolution is a plan-time step (13 section 3 rule 2)") from None


def observation_inputs(plan: PlanContext, glt_key: CacheKey, granule: GranuleRef) -> dict[str, Any]:
    """The masked-observation identity of 06 section 2 for ONE granule: `glt_key`,
    `asset_roles` (which collection/asset/variable each role reads - the bindings, not URIs,
    which vary between environments - plus that asset's catalogue checksum, the asset identity
    of 12 section 4; None when the source has none), `pixel_mask_spec` and
    `mask_plugin_version`, and `remaps`: for every categorical layer the raw class table's
    fingerprint and a hash of the resolved lookup, because the remap is applied at the gather
    (13 section 3 rule 3) and so determines the observation as much as a mask does. Nothing is
    written under this key in this slice, but its hash is what enters the snapshot key as an
    `obs_key`, so a mask, a lumping or a re-delivered table invalidates snapshots and not
    GLTs (06 section 3 rule 1)."""
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
    return {
        "artifact_type": "observation",
        "glt_key": glt_key.hash,
        "asset_roles": asset_roles,
        "pixel_mask_spec": [{"ref": m.ref, "params": dict(m.params)} for m in plan.masks],
        "mask_plugin_version": [m.version for m in plan.masks],
        "remaps": remaps,
    }


# ------------------------------------------------------------------------------------- the loop
@dataclass
class Resolved:
    """The streaming argmax over one block, trimmed to the core window."""

    layers: dict[str, np.ndarray]
    score: np.ndarray
    valid: np.ndarray
    contributing: list[str]          # granule ids that won at least one core cell


def resolve_window(plan: PlanContext, block: BlockRef, epoch: Epoch,
                   reached: list[tuple[GranuleRef, CacheKey, np.ndarray]]) -> Resolved:
    """Score every observation that reaches the block and keep the best per cell (04 section 4,
    streaming). `reached` is [(granule, glt_key, glt_window)] in candidate order. A cell is
    taken by observation i when valid, its score is not NaN, and it is strictly greater than
    the running best (or nothing has won yet): first wins a tie."""
    win = block.window
    shape = (win.height, win.width)
    best = np.full(shape, np.nan, dtype=np.float32)
    won = np.zeros(shape, dtype=bool)
    layers: dict[str, np.ndarray] = {}
    contributing: list[str] = []
    aux = NullAux()
    scorer = plan.scorer.instance
    core = _core_slices(block)
    coords = block_coords(block.transform, shape, plan.grid.crs)
    for granule, key, glt in reached:
        result = read_observation(ObsContext(plan, block, epoch, granule, key, glt, aux, coords))
        if result is None:
            continue
        obs = result.obs
        score = np.asarray(np.ma.filled(scorer.score(obs, aux), np.nan), dtype=np.float32)
        if score.shape != shape:
            raise ValueError(f"scorer {plan.scorer.ref!r} returned {score.shape}; expected "
                             f"{shape} (04 section 4)")
        take = obs.valid & ~np.isnan(score) & (~won | (score > best))
        if not take.any():
            continue
        best[take] = score[take]
        won |= take
        for layer in plan.schema.layers:
            values = result.layers[layer.name]
            if layer.name not in layers:
                bands = values.shape[2] if values.ndim == 3 else 1
                layers[layer.name] = empty_layer(layer, shape, bands)
            layers[layer.name][take] = values[take].astype(layers[layer.name].dtype)
        if take[core].any():
            contributing.append(granule.granule_id)
    for layer in plan.schema.layers:
        if layer.name not in layers:
            layers[layer.name] = empty_layer(layer, shape)
    return Resolved(layers={k: v[core] for k, v in layers.items()}, score=best[core],
                    valid=won[core], contributing=contributing)


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

    resolved = resolve_window(plan, block, epoch, reached)
    core_transform = BlockRef(tile, block.bx, block.by, halo=0).transform
    plan.cache.write_dir(snap_key, lambda d: write_snapshot(
        d, layers=resolved.layers, score=resolved.score, valid=resolved.valid,
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
    obs_keys = sorted(canonical_hash(observation_inputs(plan, key, granule))
                      for granule, key, _ in reached)
    inputs = snapshot_inputs(obs_keys, [], plan.scorer.ref, plan.scorer.version,
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
    before anything is read (06 section 2; first-slice plan section 4)."""
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
    "snapshot_key", "window_lonlat_bounds",
]
