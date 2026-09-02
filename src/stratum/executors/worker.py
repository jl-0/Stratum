"""`stratum exec`: one work item of one stage (08 section 1, "the portability seam").

Every executor - the local pool, a SLURM array task, a Lambda - reduces to `exec_item(run_dir,
stage, index)`. The plan is loaded once per process and cached; each handler recomputes the
content-addressed keys of its inputs exactly as the producing stage did, so a missing input is a
clear error and a present one is a hit whoever wrote it.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from stratum.cache import CacheKey, glt_inputs, product_inputs
from stratum.plan.document import (
    STAGES,
    PlanError,
    RunPlan,
    epoch_from_doc,
    load_run,
    read_work,
)
from stratum.publish import product_dir, publish_period, write_product_block
from stratum.reduce import band_counts, delivered_bands, reduce_stack
from stratum.regrid import REGRID_ALGO_VERSION, regrid_granule_tile
from stratum.resolve import resolve_block, snapshot_key, stack_snapshots
from stratum.types import BlockRef, Epoch, LocArray, TileRef

_RUNS: dict[Path, RunPlan] = {}


def load_cached(run_dir: Path | str) -> RunPlan:
    """The plan for `run_dir`, loaded once per process (plan.json + plugin instances)."""
    key = Path(run_dir).resolve()
    run = _RUNS.get(key)
    if run is None:
        run = _RUNS[key] = load_run(key)
    return run


def clear_cache() -> None:
    _RUNS.clear()


# ------------------------------------------------------------------------------------- handlers
def _tile(run: RunPlan, item: Mapping[str, Any]) -> TileRef:
    tx, ty = item["tile"]
    return run.tile(tx, ty)


def regrid_item(item: Mapping[str, Any], run: RunPlan) -> tuple[CacheKey, bool]:
    """`{granule_id, collection, asset, uri, tile}` -> the GLT key (03). `loc` is read through
    the AssetStore and the collection's reader only on a miss."""
    ctx = run.context
    if ctx.regrid_algo_version != REGRID_ALGO_VERSION:
        raise PlanError(f"plan was made under regrid algorithm version {ctx.regrid_algo_version} "
                        f"but this build is {REGRID_ALGO_VERSION}; re-plan (06 section 3 rule 2)")
    tile = _tile(run, item)
    gid = str(item["granule_id"])
    key = ctx.cache.key("glt", ctx.grid.id, tile, glt_inputs(
        gid, ctx.grid, ctx.max_distance, ctx.regrid_method, ctx.regrid_algo_version))
    hit = ctx.cache.hit(key)

    def loc() -> LocArray:
        reader = ctx.reader(str(item["collection"]))
        # the geolocation asset's catalogue checksum, from the frozen GranuleRef (12 section 4);
        # None for a local source, which the store accepts
        ref = ctx.granules.get(gid)
        checksum = (ref.checksums.get(f"{item['collection']}/{item['asset']}")
                    if ref is not None else None)
        rctx = reader.open(ctx.store.open(str(item["uri"]), checksum=checksum))
        try:
            arrays = reader.geolocation(rctx)
        finally:
            close = getattr(rctx, "close", None)
            if close is not None:
                close()
        if arrays is None:
            raise ValueError(f"granule {gid!r}: {item['uri']} has no geolocation; the "
                             "geolocation role must be sensor-space (03 section 3)")
        return arrays

    written = regrid_granule_tile(ctx.cache, tile, gid, loc, max_distance=ctx.max_distance,
                                  regrid_method=ctx.regrid_method)
    if written.hash != key.hash:  # the two key derivations must agree, or resolve never hits
        raise PlanError(f"regrid wrote {written.path} but resolve would look for {key.path}")
    return key, hit


def resolve_item(item: Mapping[str, Any], run: RunPlan) -> tuple[CacheKey, bool]:
    """`{tile, epoch, block}` -> the snapshot key (12 section 2)."""
    ctx = run.context
    key = snapshot_key(item, ctx)
    if ctx.cache.hit(key):
        return key, True
    return resolve_block(item, ctx), False


def product_key(item: Mapping[str, Any], run: RunPlan
                ) -> tuple[CacheKey, list[CacheKey], list[Epoch]]:
    """The product-block key of a reduce item (06 section 2) and the snapshot keys it stacks,
    recomputed as resolve computed them. Nothing is read beyond the GLT windows."""
    ctx = run.context
    tile = _tile(run, item)
    epochs = [epoch_from_doc(e) for e in item["epochs"]]
    snaps = [snapshot_key({"tile": item["tile"], "epoch": list(e.bounds), "block": item["block"]},
                          ctx) for e in epochs]
    inputs = product_inputs([k.hash for k in snaps], [], ctx.schema.aggregate_hash, None)
    return ctx.cache.key("product", ctx.grid.id, tile, inputs), snaps, epochs


def reduce_item(item: Mapping[str, Any], run: RunPlan) -> tuple[CacheKey, bool]:
    """`{tile, block, period, epochs}` -> the product-block key (13 section 4). Every listed
    epoch's snapshot must exist - the plan listed exactly the epochs that had a resolve item."""
    ctx = run.context
    key, snaps, epochs = product_key(item, run)
    if ctx.cache.hit(key):
        return key, True
    missing = [(e.bounds, k.path) for e, k in zip(epochs, snaps, strict=True)
               if not ctx.cache.hit(k)]
    if missing:
        shown = "; ".join(f"epoch {b[0]}..{b[1]}: {p}" for b, p in missing[:3])
        raise FileNotFoundError(f"resolve did not run for block {item['block']} of tile "
                                f"{item['tile']}: {len(missing)} snapshot(s) missing ({shown}); "
                                "run the resolve stage first")
    stack = stack_snapshots([k.path for k in snaps], epochs, ctx.schema)
    arrays = reduce_stack(stack)
    bands = delivered_bands(ctx.schema, band_counts=band_counts(stack))
    tile = _tile(run, item)
    bx, by = (int(v) for v in item["block"])
    block = BlockRef(tile, bx, by)
    tags = {"run_id": run.run_id, "manifest_hash": run.manifest_hash,
            "period_start": item["period"][0], "period_end": item["period"][1]}
    ctx.cache.write_dir(key, lambda d: write_product_block(d, arrays, bands, block, tags=tags))
    return key, False


def publish_item(item: Mapping[str, Any], run: RunPlan) -> tuple[Path, bool]:
    """`{tile, period}` -> `{products}/{run_id}/{tx}_{ty}/{period}/` (07, 10 section 4). The
    tile's product blocks are the reduce items of this (tile, period), each addressed by its
    recomputed key; a missing one means reduce did not run."""
    ctx = run.context
    tile = _tile(run, item)
    period = Epoch(*(epoch_from_doc(item["period"]).start, epoch_from_doc(item["period"]).end))
    blocks = [it for it in read_work(run.run_dir, "reduce")
              if list(it["tile"]) == list(item["tile"]) and list(it["period"]) == list(item["period"])]
    if not blocks:
        raise PlanError(f"no reduce items for tile {item['tile']} period {item['period']}")
    product_dirs: list[tuple[BlockRef, Path]] = []
    for it in blocks:
        key, _, _ = product_key(it, run)
        if not ctx.cache.hit(key):
            raise FileNotFoundError(f"reduce did not run for block {it['block']} of tile "
                                    f"{it['tile']}: no product block at {key.path}")
        bx, by = (int(v) for v in it["block"])
        product_dirs.append((BlockRef(tile, bx, by), key.path))
    out_dir = product_dir(run.products_dir, tile, period)
    publish_period(out_dir, product_dirs, tile, period, ctx.schema, run.outputs,
                   run_id=run.run_id, manifest_hash=run.manifest_hash,
                   band_counts=run.band_counts or None, run_dir=run.run_dir)
    return out_dir, False


HANDLERS: dict[str, Callable[[Mapping[str, Any], RunPlan], tuple[Any, bool]]] = {
    "regrid": regrid_item, "resolve": resolve_item, "reduce": reduce_item,
    "publish": publish_item,
}


# ---------------------------------------------------------------------------------- entry point
def exec_item(run_dir: Path | str, stage: str, index: int) -> dict[str, Any]:
    """The single worker entrypoint (08 section 1). Returns the outcome record the executor
    appends to `work/{stage}.results.jsonl`: `{index, stage, ok, key, hit, seconds}`."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; one of {STAGES}")
    run = load_cached(run_dir)
    items = read_work(run.run_dir, stage)
    if not 0 <= index < len(items):
        raise IndexError(f"stage {stage!r} has {len(items)} items; index {index} is out of range")
    t0 = time.perf_counter()
    result, hit = HANDLERS[stage](items[index], run)
    path = result.path if isinstance(result, CacheKey) else result
    return {"index": index, "stage": stage, "ok": True, "key": str(path), "hit": bool(hit),
            "seconds": round(time.perf_counter() - t0, 3)}


__all__ = [
    "HANDLERS", "clear_cache", "exec_item", "load_cached", "product_key", "publish_item",
    "reduce_item", "regrid_item", "resolve_item",
]
