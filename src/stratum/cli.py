"""The command surface. Reference: docs/reference/cli.html.

Implemented in this slice: plan, run (local), exec, validate, status, report, index build /
query, plugins list / show, cache explain / diff. approve, reject, render, cache stats / gc and
index verify raise NotImplementedError naming their section; so does `-p/--patch`
(09 section 3) and any executor but local (08).

Exit codes: 0 ok; 1 invalid or failed; 2 valid but over budget (plan, run).
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from stratum import __version__
from stratum.plugins import GROUPS, registered, resolve


def _guarded(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a command body; a NotImplementedError or a planning error becomes a clean message."""
    from stratum.plan import PlanError

    try:
        return fn(*args, **kwargs)
    except NotImplementedError as e:
        raise click.ClickException(f"not implemented in this slice: {e}") from None
    except PlanError as e:
        raise click.ClickException(str(e)) from None


def _todo(section: str) -> None:
    raise NotImplementedError(f"see docs/reference/cli.html#{section}")


def _set_asset_cache(path: str | None) -> None:
    """`--asset-cache` becomes `$STRATUM_ASSET_CACHE` for this process and every spawned
    worker, which is the one place `asset_cache_for` reads it (12 section 4)."""
    if path is not None:
        import os

        from stratum.access import ASSET_CACHE_ENV

        os.environ[ASSET_CACHE_ENV] = str(Path(path).expanduser().resolve())


def _run_dir(run: str, root: str | None) -> Path:
    """`--run` is a run directory, or a run id under `{root}/runs/`."""
    p = Path(run)
    if p.is_dir():
        return p
    if root is not None and (Path(root) / "runs" / run).is_dir():
        return Path(root) / "runs" / run
    raise click.ClickException(f"{run!r} is neither a run directory nor a run id under "
                               f"{root or '<--root not given>'}/runs")


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Stratum - cost-function-driven mosaics."""


# ------------------------------------------------------------------------------------------ plan
@main.command()
@click.option("-m", "--manifest", required=True, type=click.Path(exists=True))
@click.option("-p", "--patch", multiple=True)
@click.option("--out", type=click.Path())
@click.option("--asset-cache", type=click.Path(), default=None,
              help="Node-local directory the plan stages remote assets into (12 section 4); "
                   "default $STRATUM_ASSET_CACHE, else {root}/assets. Name the same one on run.")
@click.option("--json", "as_json", is_flag=True, help="Print the plan summary as JSON.")
def plan(manifest: str, patch: tuple[str, ...], out: str | None, asset_cache: str | None,
         as_json: bool) -> None:
    """Resolve a manifest, freeze the granule set, write the work list, report the fan-out."""
    from stratum.plan import plan_run

    _set_asset_cache(asset_cache)
    result = _guarded(plan_run, manifest, out, patch)
    if as_json:
        click.echo(json.dumps({"run_id": result.run_id, "run_dir": str(result.run_dir),
                               "counts": result.counts, "over_budget": result.over_budget,
                               "budget_problems": result.budget_problems}, indent=2))
    else:
        click.echo(result.report, nl=False)
        click.echo(f"\nplan written to {result.run_dir}")
    if result.over_budget:
        click.echo("OVER BUDGET: " + "; ".join(result.budget_problems), err=True)
        sys.exit(2)


@main.command()
@click.option("-m", "--manifest", type=click.Path(exists=True))
@click.option("-p", "--patch", multiple=True)
@click.option("--from-provenance", type=click.Path(exists=True))
@click.option("--executor", type=click.Choice(["local", "slurm", "aws"]), default="local")
@click.option("--workers", type=int, default=None, help="local: pool size; default one per core.")
@click.option("--out", type=click.Path(), help="Run directory; default {root}/runs/{run_id}.")
@click.option("--asset-cache", type=click.Path(), default=None,
              help="Node-local directory remote assets are staged into (12 section 4); "
                   "default $STRATUM_ASSET_CACHE, else {root}/assets.")
@click.option("--dry-run", is_flag=True, help="Equivalent to plan.")
def run(manifest: str | None, patch: tuple[str, ...], from_provenance: str | None,
        executor: str, workers: int | None, out: str | None, asset_cache: str | None,
        dry_run: bool) -> None:
    """Plan and execute every stage."""
    from stratum.executors import ExecutionError, executor_available, run_all
    from stratum.plan import BudgetExceeded, plan_run

    _set_asset_cache(asset_cache)
    _guarded(executor_available, executor)
    if from_provenance is not None:
        _guarded(_todo, "run")  # --from-provenance re-runs a frozen index: 10 section 2
    if manifest is None:
        raise click.UsageError("-m/--manifest is required")
    result = _guarded(plan_run, manifest, out, patch)
    click.echo(result.report, nl=False)
    if result.over_budget:
        click.echo("OVER BUDGET: " + "; ".join(result.budget_problems), err=True)
        if result.refused:
            sys.exit(2)
    if dry_run:
        return
    try:
        execution = run_all(result.run_dir, workers)
    except BudgetExceeded as e:
        click.echo(str(e), err=True)
        sys.exit(2)
    except ExecutionError as e:
        raise click.ClickException(str(e)) from None
    for stage, s in execution["stages"].items():
        click.echo(f"{stage:8} {s['items']:6} item(s)  {s['hits']:6} hit(s)  {s['seconds']:8.2f} s")
    click.echo(f"run {result.run_id} finished; products under "
               f"{result.document['products_dir']}")


@main.command("exec")
@click.option("--plan", "plan_dir", required=True, type=click.Path(exists=True))
@click.option("--stage", required=True,
              type=click.Choice(["regrid", "resolve", "reduce", "publish"]))
@click.option("--index", "item", required=True, type=int)
@click.option("--asset-cache", type=click.Path(), default=None,
              help="Node-local directory remote assets are staged into (12 section 4); "
                   "default $STRATUM_ASSET_CACHE, else {root}/assets.")
def exec_(plan_dir: str, stage: str, item: int, asset_cache: str | None) -> None:
    """The single worker entrypoint: one work item of one stage (08 section 1)."""
    from stratum.executors import exec_item

    _set_asset_cache(asset_cache)
    result = _guarded(exec_item, plan_dir, stage, item)
    click.echo(json.dumps(result, sort_keys=True))


@main.command()
@click.option("--run", "run_id", required=True, help="Run directory, or run id under --root.")
@click.option("--root", type=click.Path(), default=None)
@click.option("--failed", is_flag=True, help="List failed work items with their errors.")
def status(run_id: str, root: str | None, failed: bool) -> None:
    """Per-stage progress of a run, from the work lists and their results files."""
    from stratum.plan import STAGES, read_results, read_work

    run_dir = _run_dir(run_id, root)
    click.echo(f"run directory: {run_dir}")
    click.echo(f"{'stage':8} {'items':>6} {'done':>6} {'ok':>6} {'failed':>6} {'hits':>6}")
    for stage in STAGES:
        try:
            n = len(read_work(run_dir, stage))
        except FileNotFoundError:
            click.echo(f"{stage:8} (no work list)")
            continue
        results = read_results(run_dir, stage) or []
        ok = sum(1 for r in results if r.get("ok"))
        bad = [r for r in results if not r.get("ok")]
        hits = sum(1 for r in results if r.get("hit"))
        click.echo(f"{stage:8} {n:6} {len(results):6} {ok:6} {len(bad):6} {hits:6}")
        if failed:
            for r in bad:
                click.echo(f"  [{r['index']}] {r.get('error')}")


@main.command()
@click.option("--run", "run_id", required=True, help="Run directory, or run id under --root.")
@click.option("--root", type=click.Path(), default=None)
def report(run_id: str, root: str | None) -> None:
    """Print the run report (report.md)."""
    from stratum.plan import REPORT_NAME

    path = _run_dir(run_id, root) / REPORT_NAME
    if not path.is_file():
        raise click.ClickException(f"no {REPORT_NAME} under {path.parent}")
    click.echo(path.read_text(), nl=False)


@main.command()
@click.option("--run", "run_id", required=True)
def approve(run_id: str) -> None:
    """Release a run paused at the budget gate (08 section 3)."""
    _guarded(_todo, "approve")


@main.command()
@click.option("--run", "run_id", required=True)
def reject(run_id: str) -> None:
    """Cancel a run paused at the budget gate (08 section 3)."""
    _guarded(_todo, "approve")


@main.command()
@click.option("--run", "run_id", required=True)
@click.option("--mapper", required=True)
def render(run_id: str, mapper: str) -> None:
    """Re-render from published data without re-running the pipeline (07 section 5)."""
    _guarded(_todo, "render")


# ----------------------------------------------------------------------------------------- cache
@main.group()
def cache() -> None:
    """Content-addressed artifacts (06 section 6)."""


@cache.command("explain")
@click.argument("key", type=click.Path(exists=True))
def cache_explain(key: str) -> None:
    """Print the .inputs.json that produced KEY (an artifact path, its directory or the sidecar)."""
    from stratum.cache import CacheRoot

    path = Path(key)
    try:
        doc = CacheRoot(path.parent).explain(path)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from None
    click.echo(json.dumps(doc, indent=2, sort_keys=True))


@cache.command("diff")
@click.argument("key_a", type=click.Path(exists=True))
@click.argument("key_b", type=click.Path(exists=True))
def cache_diff(key_a: str, key_b: str) -> None:
    """Which inputs differ between two artifacts - the answer to "why did this miss?"."""
    from stratum.cache import CacheRoot

    try:
        diff = CacheRoot(Path(key_a).parent).diff(Path(key_a), Path(key_b))
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from None
    if not diff:
        click.echo("identical inputs")
    for field, (a, b) in diff.items():
        click.echo(f"{field}: {a!r} -> {b!r}")


@cache.command("stats")
def cache_stats() -> None:
    _guarded(_todo, "cache")


@cache.command("gc")
def cache_gc() -> None:
    _guarded(_todo, "cache")


# ----------------------------------------------------------------------------------------- index
@main.group()
def index() -> None:
    """The granule index: built ahead of a run, never queried live (12 section 5)."""


@index.command("build")
@click.option("-m", "--manifest", required=True, type=click.Path(exists=True))
@click.option("--collection", multiple=True, help="Restrict to these collections.")
@click.option("--since", type=click.DateTime(), default=None,
              help="Refresh an existing index: fetch only rows revised since this instant (a "
                   "local source: file mtime) and merge them in; every other row is kept.")
def index_build(manifest: str, collection: tuple[str, ...], since: datetime | None) -> None:
    """Populate inputs.index_location from inputs.source: a local directory, or CMR scoped to
    the manifest's AOI and time range (12 section 5). Metadata only - no pixel is fetched.
    With --since the revised rows replace their earlier versions in the existing index."""
    from stratum.index import read_index
    from stratum.manifest import load_manifest
    from stratum.plan import build_index_from_manifest

    m = load_manifest(manifest)
    src = m.inputs.source
    if src is not None:
        click.echo(f"source: {src.kind}" + (f" ({src.provider})" if src.provider else ""))
    path = _guarded(build_index_from_manifest, m, collections=collection or None, since=since)
    frame = read_index(path)
    click.echo(f"wrote {path}: {len(frame)} row(s), {frame['granule_id'].nunique()} granule(s)")
    for name, rows in frame.groupby("collection", sort=True):
        versions = ", ".join(sorted(set(rows["collection_version"].astype(str))))
        sums = sum(1 for c in rows["checksums"] if c)
        click.echo(f"  {name:14} {len(rows):6} row(s)  version {versions or '-':6}  "
                   f"{sums} with checksums  {rows['datetime'].min():%Y-%m-%dT%H:%M} .. "
                   f"{rows['datetime'].max():%Y-%m-%dT%H:%M}")
    if len(frame):
        click.echo(f"time span: {frame['datetime'].min().isoformat()} .. "
                   f"{frame['datetime'].max().isoformat()}")


@index.command("query")
@click.option("--index", "index_path", required=True, type=click.Path(exists=True),
              help="An inputs.index_location directory, or a parquet file.")
@click.option("--bbox", default=None, help="W,S,E,N in degrees.")
@click.option("--start", type=click.DateTime(), default=None)
@click.option("--end", type=click.DateTime(), default=None)
@click.option("--collection", multiple=True)
@click.option("--limit", type=int, default=10, help="Rows to print.")
def index_query(index_path: str, bbox: str | None, start: datetime | None, end: datetime | None,
                collection: tuple[str, ...], limit: int) -> None:
    """Ad-hoc query against a built index: a count and the first rows."""
    from stratum.index import query_index, read_index

    box = None
    if bbox:
        parts = [float(v) for v in bbox.split(",")]
        if len(parts) != 4:
            raise click.UsageError("--bbox is W,S,E,N")
        box = (parts[0], parts[1], parts[2], parts[3])
    from pathlib import Path as _P

    from stratum.index import INDEX_FILE

    target = _P(index_path)
    if target.is_dir():
        target = target / INDEX_FILE
    frame = query_index(read_index(target), bbox=box, start=start, end=end,
                        collections=collection or None)
    click.echo(f"{len(frame)} row(s), {frame['granule_id'].nunique()} granule(s)")
    for _, row in frame.head(limit).iterrows():
        w, s, e, n = row["bbox"]
        click.echo(f"{row['granule_id']:34} {row['collection']:12} "
                   f"{row['datetime'].isoformat()}  [{w:.3f}, {s:.3f}, {e:.3f}, {n:.3f}]  "
                   f"build {row['build_version'] or '-'}")


@index.command("verify")
@click.option("-m", "--manifest", required=True, type=click.Path(exists=True))
def index_verify(manifest: str) -> None:
    _guarded(_todo, "index")


# --------------------------------------------------------------------------------------- plugins
@main.group()
def plugins() -> None:
    """What this environment provides."""


@plugins.command("list")
@click.option("--kind", type=click.Choice(sorted(GROUPS)), default=None)
def plugins_list(kind: str | None) -> None:
    for k, group in GROUPS.items():
        if kind is not None and k != kind:
            continue
        found = registered(k)
        click.echo(f"{k:8} ({group})")
        for name, target in sorted(found.items()):
            click.echo(f"  {name:20} {target}")
        if not found:
            click.echo("  (none)")


_SHOWN = ("capability", "halo", "required_roles", "required_aux", "space", "outputs",
          "collections")


@plugins.command("show")
@click.argument("ref")
@click.option("--kind", type=click.Choice(sorted(GROUPS)), default=None,
              help="Which registry to look the name up in; tried in turn when omitted.")
def plugins_show(ref: str, kind: str | None) -> None:
    """Resolve REF (entry-point name or module:Class) and print what it declares."""
    from stratum.resolve import plugin_version

    kinds = [kind] if kind else list(GROUPS)
    cls = None
    for k in kinds:
        try:
            cls = resolve(k, ref)
            kind = k
            break
        except (LookupError, ImportError, AttributeError):
            continue
    if cls is None:
        raise click.ClickException(f"{ref!r} does not resolve as any of {kinds}")
    click.echo(f"ref:      {ref}")
    click.echo(f"kind:     {kind}")
    click.echo(f"class:    {cls.__qualname__}")
    click.echo(f"module:   {cls.__module__}")
    click.echo(f"version:  {plugin_version(cls)}")
    for attr in _SHOWN:
        if hasattr(cls, attr):
            click.echo(f"{attr + ':':18}{getattr(cls, attr)!r}")
    doc = (cls.__doc__ or "").strip().splitlines()
    if doc:
        click.echo(f"doc:      {doc[0]}")


# -------------------------------------------------------------------------------------- validate
@main.command()
@click.option("-m", "--manifest", required=True, type=click.Path(exists=True))
@click.option("-p", "--patch", multiple=True)
def validate(manifest: str, patch: tuple[str, ...]) -> None:
    """Everything spec 09 section 5 checks without data, and nothing else."""
    from pydantic import ValidationError

    from stratum.manifest import load_manifest, validate_static

    try:
        m = _guarded(load_manifest, manifest, patch)
    except ValidationError as e:
        raise click.ClickException(f"{manifest} is not a valid manifest:\n{e}") from None
    except (ValueError, TypeError) as e:
        raise click.ClickException(f"{manifest}: {e}") from None
    problems = validate_static(m)
    if problems:
        click.echo(f"{manifest}: {len(problems)} problem(s)")
        for p in problems:
            click.echo(f"  - {p}")
        sys.exit(1)
    click.echo(f"{manifest}: ok (run_id {m.run_id})")
