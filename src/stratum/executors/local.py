"""The `local` executor: a process pool over the work lists (08 section 1).

    run_stage(run_dir, "regrid")    every item of one stage, through `exec_item`
    run_all(run_dir)                regrid -> resolve -> reduce -> publish -> Finalize

Locally a run is all-or-nothing: any failed item stops the run with the failures listed (08
section 4's tolerated-failure percentage is a later slice). Outcomes go to
`work/{stage}.results.jsonl` either way, so `stratum status` can say what happened.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import time
import traceback
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stratum import __version__
from stratum.executors.worker import exec_item, load_cached
from stratum.plan.document import (
    REPORT_NAME,
    STAGES,
    BudgetExceeded,
    RunPlan,
    read_plan,
    read_work,
    results_path,
)
from stratum.publish import build_provenance, write_provenance, write_stac_collection


class ExecutionError(RuntimeError):
    """One or more work items failed; the message lists them."""


def _attempt(run_dir: str, stage: str, index: int) -> dict[str, Any]:
    """`exec_item` with the exception folded into the outcome record."""
    try:
        return exec_item(run_dir, stage, index)
    except Exception as e:  # noqa: BLE001 - every failure becomes a queryable outcome
        return {"index": index, "stage": stage, "ok": False,
                "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}


def run_stage(run_dir: Path | str, stage: str, workers: int | None = None) -> list[dict[str, Any]]:
    """Execute every item of `stage`. `workers` defaults to the CPU count; 1 runs in-process
    (the same `exec_item` code path, handy under a debugger), more uses a spawn-context pool.
    Writes the outcomes and raises `ExecutionError` if any item failed."""
    run_dir = Path(run_dir).resolve()
    items = read_work(run_dir, stage)
    n = len(items)
    workers = workers or os.cpu_count() or 1
    results: list[dict[str, Any]] = []
    if n and (workers <= 1 or n == 1):
        results = [_attempt(str(run_dir), stage, i) for i in range(n)]
    elif n:
        mp = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(workers, n), mp_context=mp) as pool:
            futures = {pool.submit(_attempt, str(run_dir), stage, i): i for i in range(n)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001 - a worker that died before returning
                    results.append({"index": i, "stage": stage, "ok": False,
                                    "error": f"{type(e).__name__}: {e}"})
    results.sort(key=lambda r: r["index"])
    path = results_path(run_dir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in results))
    failed = [r for r in results if not r["ok"]]
    if failed:
        shown = "\n".join(f"  [{r['index']}] {r['error']}" for r in failed[:10])
        more = f"\n  ... and {len(failed) - 10} more" if len(failed) > 10 else ""
        first_tb = failed[0].get("traceback", "")
        raise ExecutionError(f"stage {stage!r}: {len(failed)} of {n} item(s) failed "
                             f"(see {path}):\n{shown}{more}\n\nfirst failure:\n{first_tb}")
    return results


def budget_gate(run: RunPlan | Mapping[str, Any]) -> None:
    """09 section 4: over budget stops a run unless `on_exceed: warn`. `require_approval`
    would park it for `stratum approve` (08 section 3), which is a later slice, so it refuses.
    Accepts the raw `plan.json` document as well as a `RunPlan`: a refused plan carries no
    worker context (the planner stops before inspecting or staging anything), so the gate has
    to run before `load_run` would try to rebuild one."""
    doc = run.document if isinstance(run, RunPlan) else run
    b = doc["budget"]
    if not b.get("over"):
        return
    if b.get("on_exceed") == "warn":
        return
    why = "; ".join(b.get("problems", []))
    hint = ("stratum approve is a later slice (08 section 3); set budget.on_exceed: warn to "
            "proceed" if b.get("on_exceed") == "require_approval" else "budget.on_exceed is fail")
    raise BudgetExceeded(f"run {doc['run_id']} is over budget: {why}. {hint}")


def run_all(run_dir: Path | str, workers: int | None = None) -> dict[str, Any]:
    """Every stage in order, then Finalize: the STAC collection, `provenance.json` (10 section
    2) and an execution section appended to `report.md`. Returns the execution record."""
    run_dir = Path(run_dir).resolve()
    budget_gate(read_plan(run_dir))
    run = load_cached(run_dir)
    started = datetime.now(UTC)
    execution: dict[str, Any] = {"stages": {}, "cache_hits": {}, "failed_items": []}
    for stage in STAGES:
        t0 = time.perf_counter()
        results = run_stage(run_dir, stage, workers)
        execution["stages"][stage] = {"items": len(results),
                                      "hits": sum(1 for r in results if r.get("hit")),
                                      "seconds": round(time.perf_counter() - t0, 3)}
        execution["cache_hits"][stage] = execution["stages"][stage]["hits"]
    finished = datetime.now(UTC)

    if run.outputs.get("stac", True):
        items = sorted(run.products_dir.glob("*/*/item.json"))
        if items:
            write_stac_collection(run.products_dir, items, description=run.document.get(
                "description") or f"Stratum products for run {run.run_id}")

    counts = run.document["counts"]
    store_cache = getattr(run.context.store, "asset_cache", None)
    execution.update({"tiles": counts["tiles"], "epochs": counts["epochs"],
                      "blocks": counts["blocks"], "workers": workers or os.cpu_count() or 1,
                      "executor": "local",
                      # where remote assets were staged (12 section 4): recorded so a reader of
                      # the provenance knows where the bytes went; it enters no cache key
                      "asset_cache": str(store_cache) if store_cache is not None else None})
    record = provenance_record(run, started, finished, execution)
    prov = write_provenance(run_dir, record)
    with (run_dir / REPORT_NAME).open("a") as fh:
        fh.write(render_execution(execution, prov))
    return execution


def provenance_record(run: RunPlan, started: datetime, finished: datetime,
                      execution: dict[str, Any]) -> dict[str, Any]:
    """The 10 section 2 record from the plan document and the execution counts."""
    doc = run.document
    ctx = run.context
    role_collection = {name: b.collection for name, b in ctx.roles.items()}
    class_tables: dict[str, list[str]] = {}
    for layer in ctx.schema.layers:
        fps = doc["class_tables"].get(layer.name)
        if fps:
            src = ctx.role_of(layer.source)
            col = role_collection[src]
            class_tables[col] = sorted(set(class_tables.get(col, [])) | set(fps))
    mappers = {name: {"ref": cfg.get("mapper"), "version": __version__}
               for name, cfg in (run.outputs.get("render") or {}).items()}
    return build_provenance(
        run_id=run.run_id, manifest_hash=run.manifest_hash, manifest=_merged_manifest(run),
        started=started, finished=finished,
        inputs={"frozen_index": doc["index"]["frozen"], "frozen_index_hash": doc["index"]["hash"],
                "granule_count": doc["index"]["granule_count"],
                "build_versions": doc["build_versions"], "class_tables": class_tables,
                "collections": {c: v[0] if len(v) == 1 else v
                                for c, v in doc["collection_versions"].items()},
                # asset identity (12 section 4): granule -> asset -> catalogue checksum, for
                # the granules that have any; a local source records none, honestly
                "asset_checksums": {gid: dict(ref.checksums)
                                    for gid, ref in ctx.granules.items() if ref.checksums}},
        code={"regrid_algo_version": ctx.regrid_algo_version},
        plugins={"scorer": ctx.scorer.identity(),
                 "masks": [m.identity() for m in ctx.masks],
                 "reducer": {"ref": "schema", "version": __version__},
                 "mappers": mappers},
        schema={"name": ctx.schema.name, "layers_hash": ctx.schema.layers_hash,
                "aggregate_hash": ctx.schema.aggregate_hash,
                "extends": list(ctx.schema.extends)},
        filters=doc["filters"], execution=execution)


def _merged_manifest(run: RunPlan) -> dict[str, Any]:
    import yaml  # local: the merged document is only needed here

    path = run.run_dir / "manifest.merged.yaml"
    return yaml.safe_load(path.read_text()) if path.is_file() else {}


def render_execution(execution: dict[str, Any], provenance: Path) -> str:
    lines = ["", "## Execution", "",
             f"- executor: {execution['executor']} ({execution['workers']} worker(s))", "",
             "| stage | items | cache hits | seconds |", "|---|---|---|---|"]
    for stage, s in execution["stages"].items():
        lines.append(f"| {stage} | {s['items']} | {s['hits']} | {s['seconds']} |")
    lines += ["", f"- failed items: {len(execution['failed_items'])}",
              f"- provenance: `{provenance}`"]
    return "\n".join(lines) + "\n"


__all__ = ["ExecutionError", "budget_gate", "provenance_record", "render_execution", "run_all",
           "run_stage"]
