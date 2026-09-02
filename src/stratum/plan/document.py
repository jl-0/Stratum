"""`plan.json` and the work lists: what a worker needs beyond the manifest (first-slice plan
section 4), and how a worker rebuilds its `PlanContext` from them.

The run directory (plan section 4, storage layout):

    {run_dir}/manifest.merged.yaml   the document that was hashed
    {run_dir}/index.parquet          the frozen candidate set (02 section 6)
    {run_dir}/plan.json              this document
    {run_dir}/work/{stage}.jsonl     one work item per line
    {run_dir}/work/{stage}.results.jsonl   one outcome per item, written by the executor
    {run_dir}/report.md              the dry-run report, extended by the executor
    {run_dir}/provenance.json        written by Finalize (10 section 2)

Everything here is plain JSON: paths, ISO datetimes, class tables as columns, remaps as
integer lists. Plugin instances are rebuilt from `{ref, params}` in the worker and carry the
version the planner recorded, so a worker's keys are the planner's keys.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from stratum.access import AssetStore
from stratum.cache import CacheRoot
from stratum.classes import Remap
from stratum.plugins import resolve
from stratum.resolve import AliasBinding, PlanContext, PluginBinding, RoleBinding
from stratum.time import DeliveryPeriod
from stratum.types import (
    Aggregation,
    ClassTable,
    Epoch,
    GranuleRef,
    GridDef,
    LayerSpec,
    SnapshotSchema,
    TileRef,
)

PLAN_NAME = "plan.json"
WORK_DIR = "work"
REPORT_NAME = "report.md"
MERGED_NAME = "manifest.merged.yaml"
STAGES: tuple[str, ...] = ("regrid", "resolve", "reduce", "publish")
PLAN_SCHEMA_VERSION = "1.0"


class PlanError(RuntimeError):
    """The plan stage refused: validation, data, or budget (09 section 5)."""


class BudgetExceeded(PlanError):
    """The fan-out exceeds the manifest's budget and `on_exceed` does not say `warn`
    (09 section 4)."""


# ------------------------------------------------------------------------------- serialisation
def iso(when: datetime) -> str:
    return when.isoformat()


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(str(text))


def epoch_to_doc(epoch: Epoch) -> list[str]:
    return list(epoch.bounds)


def epoch_from_doc(doc: Sequence[str]) -> Epoch:
    start, end = doc
    return Epoch(from_iso(start), from_iso(end))


def period_to_doc(period: DeliveryPeriod) -> dict[str, Any]:
    return {"start": iso(period.start), "end": iso(period.end),
            "epochs": [epoch_to_doc(e) for e in period.epochs]}


def period_from_doc(doc: Mapping[str, Any]) -> DeliveryPeriod:
    return DeliveryPeriod(from_iso(doc["start"]), from_iso(doc["end"]),
                          tuple(epoch_from_doc(e) for e in doc["epochs"]))


def class_table_to_doc(table: ClassTable) -> dict[str, Any]:
    """Columns as lists. The fingerprint is over `to_pylist()` values, so a table rebuilt from
    this has the same fingerprint whatever arrow type the original columns had."""
    return {"key": table.key, "source": table.source,
            "columns": {c: table.entries.column(c).to_pylist()
                        for c in table.entries.column_names}}


def class_table_from_doc(doc: Mapping[str, Any]) -> ClassTable:
    return ClassTable(key=doc["key"], entries=pa.table(dict(doc["columns"])),
                      source=doc["source"])


def schema_to_doc(schema: SnapshotSchema) -> dict[str, Any]:
    return {
        "name": schema.name,
        "extends": list(schema.extends),
        "layers_hash": schema.layers_hash,
        "aggregate_hash": schema.aggregate_hash,
        "layers": [{
            "name": layer.name, "kind": layer.kind, "source": layer.source,
            "dtype": layer.dtype,
            "bands": list(layer.bands) if layer.bands is not None else None,
            "aggregate": {"method": layer.aggregate.method,
                          "params": dict(layer.aggregate.params)},
            "classes": class_table_to_doc(layer.classes) if layer.classes is not None else None,
            "lumping": layer.lumping,
        } for layer in schema.layers],
    }


def schema_from_doc(doc: Mapping[str, Any]) -> SnapshotSchema:
    """Rebuild the resolved schema; refuses when the rebuilt `layers_hash` is not the recorded
    one, because every snapshot key of the run depends on it (13 section 5)."""
    layers = [LayerSpec(
        name=d["name"], kind=d["kind"], source=d["source"],
        aggregate=Aggregation(d["aggregate"]["method"], dict(d["aggregate"]["params"])),
        dtype=d.get("dtype"),
        bands=tuple(d["bands"]) if d.get("bands") is not None else None,
        classes=class_table_from_doc(d["classes"]) if d.get("classes") is not None else None,
        lumping=d.get("lumping"),
    ) for d in doc["layers"]]
    schema = SnapshotSchema(name=doc["name"], layers=tuple(layers),
                            extends=tuple(doc.get("extends", ())))
    if schema.layers_hash != doc["layers_hash"]:
        raise PlanError(f"plan.json schema {doc['name']!r}: rebuilt layers_hash "
                        f"{schema.layers_hash} != recorded {doc['layers_hash']} (13 section 5)")
    return schema


def granule_to_doc(ref: GranuleRef) -> dict[str, Any]:
    doc = asdict(ref)
    doc["datetime"] = iso(ref.datetime)
    doc["end_datetime"] = iso(ref.end_datetime)
    doc["bbox"] = [float(v) for v in ref.bbox]
    doc["assets"] = dict(ref.assets)
    doc["attributes"] = dict(ref.attributes)
    return doc


def granule_from_doc(doc: Mapping[str, Any]) -> GranuleRef:
    d = dict(doc)
    d["datetime"] = from_iso(d["datetime"])
    d["end_datetime"] = from_iso(d["end_datetime"])
    d["bbox"] = tuple(float(v) for v in d["bbox"])
    return GranuleRef(**d)


def remaps_to_doc(remaps: Mapping[str, Mapping[str, Remap]]) -> dict[str, Any]:
    """Per layer: the distinct lookups by raw fingerprint, and which granule uses which - one
    copy of a 300-entry lookup per vintage rather than one per granule."""
    out: dict[str, Any] = {}
    for layer, by_granule in remaps.items():
        tables: dict[str, Any] = {}
        granules: dict[str, str] = {}
        for gid, remap in by_granule.items():
            tables.setdefault(remap.raw_fingerprint, {
                "lookup": [int(v) for v in remap.lookup.tolist()],
                "enumeration": remap.enumeration})
            granules[gid] = remap.raw_fingerprint
        out[layer] = {"tables": tables, "granules": granules}
    return out


def remaps_from_doc(doc: Mapping[str, Any]) -> dict[str, dict[str, Remap]]:
    out: dict[str, dict[str, Remap]] = {}
    for layer, d in doc.items():
        tables = {fp: Remap(lookup=np.asarray(t["lookup"], dtype=np.int32), raw_fingerprint=fp,
                            enumeration=t["enumeration"]) for fp, t in d["tables"].items()}
        out[layer] = {gid: tables[fp] for gid, fp in d["granules"].items()}
    return out


def plugin_to_doc(binding: PluginBinding) -> dict[str, Any]:
    return binding.identity()


def plugin_from_doc(kind: str, doc: Mapping[str, Any]) -> PluginBinding:
    """Instantiate `{ref, params}` and carry the recorded `version`: the planner's identity is
    what enters the keys, not whatever this process would compute (06 section 3 rule 3)."""
    cls = resolve(kind, doc["ref"])
    return PluginBinding(ref=doc["ref"], version=doc["version"], params=dict(doc["params"]),
                         instance=cls(**dict(doc["params"])))


def grid_to_doc(grid: GridDef) -> dict[str, Any]:
    return {"crs": grid.crs, "resolution": list(grid.resolution), "origin": list(grid.origin),
            "tile_size": grid.tile_size, "block_size": grid.block_size}


def grid_from_doc(doc: Mapping[str, Any]) -> GridDef:
    return GridDef(crs=doc["crs"], resolution=tuple(doc["resolution"]),
                   origin=tuple(doc["origin"]), tile_size=doc["tile_size"], block_size=doc["block_size"])


def context_to_doc(ctx: PlanContext) -> dict[str, Any]:
    """The `PlanContext` fields as JSON (every field its docstring lists, minus cache/store,
    which the worker builds from the root)."""
    return {
        "grid": grid_to_doc(ctx.grid),
        "grid_id": ctx.grid.id,
        "granules": {gid: granule_to_doc(ref) for gid, ref in ctx.granules.items()},
        "roles": {name: {"collection": b.collection, "var": b.var, "asset": b.asset}
                  for name, b in ctx.roles.items()},
        "aliases": {name: {"role": a.role, "band": a.band} for name, a in ctx.aliases.items()},
        "geolocation_role": ctx.geolocation_role,
        "schema": schema_to_doc(ctx.schema),
        "scorer": plugin_to_doc(ctx.scorer),
        "masks": [plugin_to_doc(m) for m in ctx.masks],
        "remaps": remaps_to_doc(ctx.remaps),
        "max_distance": float(ctx.max_distance),
        "regrid_method": ctx.regrid_method,
        "regrid_algo_version": int(ctx.regrid_algo_version),
        "reader_overrides": dict(ctx.reader_overrides),
    }


def context_from_doc(doc: Mapping[str, Any], root: Path) -> PlanContext:
    """The worker's `PlanContext`: a real `CacheRoot` under `root` and a real `AssetStore`."""
    return PlanContext(
        grid=grid_from_doc(doc["grid"]),
        cache=CacheRoot(root),
        store=AssetStore(),
        granules={gid: granule_from_doc(g) for gid, g in doc["granules"].items()},
        roles={name: RoleBinding(**r) for name, r in doc["roles"].items()},
        aliases={name: AliasBinding(**a) for name, a in doc["aliases"].items()},
        geolocation_role=doc["geolocation_role"],
        schema=schema_from_doc(doc["schema"]),
        scorer=plugin_from_doc("scorer", doc["scorer"]),
        masks=[plugin_from_doc("mask", m) for m in doc["masks"]],
        remaps=remaps_from_doc(doc["remaps"]),
        max_distance=float(doc["max_distance"]),
        regrid_method=doc["regrid_method"],
        regrid_algo_version=int(doc["regrid_algo_version"]),
        reader_overrides=dict(doc.get("reader_overrides", {})),
    )


# ------------------------------------------------------------------------------------ the run
@dataclass
class RunPlan:
    """A loaded `plan.json`: the worker context plus what the stages need around it."""

    run_id: str
    run_label: str
    manifest_hash: str
    manifest_path: Path
    root: Path
    run_dir: Path
    products_dir: Path
    context: PlanContext
    outputs: dict[str, Any]
    tiles: list[TileRef]
    epochs: list[Epoch]
    periods: list[DeliveryPeriod]
    band_counts: dict[str, int]
    budget: dict[str, Any]
    document: dict[str, Any]

    @property
    def index_hash(self) -> str:
        return str(self.document["index"]["hash"])

    def tile(self, tx: int, ty: int) -> TileRef:
        return TileRef(self.context.grid, int(tx), int(ty))


def write_plan(run_dir: Path, doc: Mapping[str, Any]) -> Path:
    path = Path(run_dir) / PLAN_NAME
    path.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n")
    return path


def read_plan(run_dir: Path) -> dict[str, Any]:
    path = Path(run_dir) / PLAN_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {PLAN_NAME} under {run_dir}; run `stratum plan` first")
    return json.loads(path.read_text())


def load_run(run_dir: Path) -> RunPlan:
    """Read `plan.json` and rebuild everything a stage handler needs."""
    run_dir = Path(run_dir).resolve()
    doc = read_plan(run_dir)
    if doc.get("plan_schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanError(f"{run_dir / PLAN_NAME}: plan_schema_version "
                        f"{doc.get('plan_schema_version')!r} is not {PLAN_SCHEMA_VERSION!r}")
    root = Path(doc["root"])
    ctx = context_from_doc(doc["context"], root)
    return RunPlan(
        run_id=doc["run_id"], run_label=doc["run_label"], manifest_hash=doc["manifest_hash"],
        manifest_path=Path(doc["manifest_path"]), root=root, run_dir=run_dir,
        products_dir=Path(doc["products_dir"]), context=ctx, outputs=dict(doc["outputs"]),
        tiles=[TileRef(ctx.grid, int(tx), int(ty)) for tx, ty in doc["tiles"]],
        epochs=[epoch_from_doc(e) for e in doc["epochs"]],
        periods=[period_from_doc(p) for p in doc["periods"]],
        band_counts={k: int(v) for k, v in doc.get("band_counts", {}).items()},
        budget=dict(doc["budget"]), document=doc,
    )


# ------------------------------------------------------------------------------------- work lists
def work_path(run_dir: Path, stage: str) -> Path:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; one of {STAGES}")
    return Path(run_dir) / WORK_DIR / f"{stage}.jsonl"


def results_path(run_dir: Path, stage: str) -> Path:
    return work_path(run_dir, stage).with_name(f"{stage}.results.jsonl")


def write_work(run_dir: Path, stage: str, items: Sequence[Mapping[str, Any]]) -> Path:
    path = work_path(run_dir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(dict(it), sort_keys=True) + "\n" for it in items))
    return path


def read_work(run_dir: Path, stage: str) -> list[dict[str, Any]]:
    path = work_path(run_dir, stage)
    if not path.is_file():
        raise FileNotFoundError(f"no work list for stage {stage!r} at {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_results(run_dir: Path, stage: str) -> list[dict[str, Any]] | None:
    """Per-item outcomes the executor wrote, or None when the stage has not run."""
    path = results_path(run_dir, stage)
    if not path.is_file():
        return None
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


__all__ = [
    "MERGED_NAME", "PLAN_NAME", "PLAN_SCHEMA_VERSION", "REPORT_NAME", "STAGES", "WORK_DIR",
    "BudgetExceeded", "PlanError", "RunPlan", "class_table_from_doc", "class_table_to_doc",
    "context_from_doc", "context_to_doc", "epoch_from_doc", "epoch_to_doc", "granule_from_doc",
    "granule_to_doc", "grid_from_doc", "grid_to_doc", "load_run", "period_from_doc",
    "period_to_doc", "plugin_from_doc", "plugin_to_doc", "read_plan", "read_results",
    "read_work", "remaps_from_doc", "remaps_to_doc", "results_path", "schema_from_doc",
    "schema_to_doc", "work_path", "write_plan", "write_work",
]
