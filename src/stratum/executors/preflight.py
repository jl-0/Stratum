"""Preflight: can THIS executor, on THIS machine or deployment, run the plan? (08 section 1).

`stratum plan` proves the manifest and the data: references resolve, granules carry what the
roles read, class tables agree. It deliberately knows nothing about where the run will execute,
because one plan runs on several executors. These checks are the other half - the environment -
and run after the plan, against its estimate, before any work item is dispatched:

    local   the storage root's volume holds what the run will write; the asset cache's volume
            can hold a working set; the wall time at this machine's core count
    aws     the credentials are live now (an expired session token is the commonest failure);
            the worker function exists, and its timeout clears the longest estimated item; the
            in-flight count fits its reserved concurrency; the dollar estimate at its real memory

A `fail` stops `stratum run` before dispatch; a `warn` is printed and the run proceeds.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from stratum.plan.estimate import (
    LAMBDA_SLOWDOWN,
    ROM_HIGH,
    fmt_bytes,
    fmt_hours,
    lambda_cost,
)
from stratum.storage import evict_reserve, parse_s3, scratch_dir

Level = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


def preflight(doc: Mapping[str, Any], executor: str, *, workers: int | None = None,
              lambda_client: Any | None = None, sts_client: Any | None = None) -> list[Check]:
    """Every check for `executor` against a written plan document. Never raises for a failed
    check - it returns one; `failed()` decides."""
    rom = doc.get("estimate")
    if rom is None:
        return [Check("estimate", "warn", "the plan carries no estimate (refused at the budget "
                                          "gate, or written before estimates existed)")]
    if executor == "aws":
        return _aws(doc, rom, workers, lambda_client, sts_client)
    return _local(doc, rom, workers)


def failed(checks: Sequence[Check]) -> list[Check]:
    return [c for c in checks if c.level == "fail"]


def render(checks: Sequence[Check], executor: str) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    lines = [f"## Preflight ({executor})", ""]
    lines += [f"- {mark[c.level]} {c.name}: {c.detail}" for c in checks]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------ local
def wall_hours(rom: Mapping[str, Any], parallel: int, *, scale: float = 1.0,
               slowdown: float = 1.0) -> float:
    """Stages run one after another, and a stage can finish no sooner than its longest item:
    the sum over stages of max(CPU time / workers, longest item)."""
    secs = sum(max(rom["cpu_seconds"][s] * scale * slowdown / max(1, parallel),
                   rom["longest_item_seconds"][s] * scale * slowdown)
               for s in rom["cpu_seconds"])
    return secs / 3600


def _free(path: Path) -> int:
    """Free bytes on the volume holding `path`, walking up to the nearest existing parent."""
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def _local(doc: Mapping[str, Any], rom: Mapping[str, Any], workers: int | None) -> list[Check]:
    checks: list[Check] = []
    reserve = evict_reserve()
    written = int(sum(rom["footprint_bytes"].values()))
    root = str(doc["root"])
    if parse_s3(root) is None:
        free = _free(Path(root))
        need = written + reserve
        level: Level = ("fail" if need > free else "warn" if need > free / 2 else "ok")
        checks.append(Check("disk: storage root", level,
                            f"~{fmt_bytes(written)} to write (x{ROM_HIGH:g}: "
                            f"{fmt_bytes(written * ROM_HIGH)}), {fmt_bytes(free)} free on "
                            f"{root}. A local root is the record and is never evicted"))
    else:
        free = _free(scratch_dir())
        checks.append(Check("disk: storage mirror", "ok" if free > 2 * reserve else "warn",
                            f"the bucket holds the {fmt_bytes(written)}; the local mirror under "
                            f"{scratch_dir()} is evicted to keep {fmt_bytes(reserve)} free "
                            f"({fmt_bytes(free)} free now)"))
    cache = (doc.get("staging") or {}).get("asset_cache")
    downloads = rom.get("download_bytes")
    if cache:
        free = _free(Path(cache))
        if downloads is None:
            checks.append(Check("disk: asset cache", "ok",
                                f"download volume unknown (a local source); {fmt_bytes(free)} "
                                f"free at {cache}"))
        else:
            level = "ok" if downloads + reserve < free else "warn"
            checks.append(Check("disk: asset cache", level,
                                f"{fmt_bytes(downloads)} of granules, {fmt_bytes(free)} free at "
                                f"{cache}" + ("" if level == "ok" else
                                              "; the cache evicts least-recently-used files to "
                                              "keep room, so the run proceeds but re-downloads "
                                              "what it evicted")))
    cores = workers or os.cpu_count() or 1
    checks.append(Check("wall time", "ok",
                        f"~{fmt_hours(wall_hours(rom, cores))} on {cores} worker(s) "
                        f"({fmt_hours(wall_hours(rom, cores, scale=0.5))} - "
                        f"{fmt_hours(wall_hours(rom, cores, scale=ROM_HIGH))}), compute only - "
                        "downloads come on top"))
    return checks


# -------------------------------------------------------------------------------------------- aws
def _aws(doc: Mapping[str, Any], rom: Mapping[str, Any], workers: int | None,
         lambda_client: Any | None, sts_client: Any | None) -> list[Check]:
    from stratum.executors.aws import AwsExecutorError, concurrency, function_name

    checks: list[Check] = []
    try:
        sts = sts_client if sts_client is not None else _boto("sts")
        who = sts.get_caller_identity()
        checks.append(Check("credentials", "ok", f"account {who.get('Account')}"))
    except Exception as e:  # noqa: BLE001 - every failure is a finding, named by its code
        checks.append(Check("credentials", "fail",
                            f"{_code(e)}: refresh the session (aws sso login / a new token) "
                            "before dispatching"))
        return checks
    try:
        fn = function_name()
    except AwsExecutorError as e:
        checks.append(Check("worker function", "fail", str(e)))
        return checks
    lam = lambda_client if lambda_client is not None else _boto("lambda")
    try:
        cfg = lam.get_function_configuration(FunctionName=fn)
    except Exception as e:  # noqa: BLE001
        checks.append(Check("worker function", "fail", f"{fn}: {_code(e)}"))
        return checks
    memory, timeout = int(cfg.get("MemorySize", 0)), int(cfg.get("Timeout", 0))
    checks.append(Check("worker function", "ok",
                        f"{fn}: {memory} MB, {timeout} s timeout, "
                        f"{'/'.join(cfg.get('Architectures') or ['?'])}, last modified "
                        f"{cfg.get('LastModified', '?')}. It runs the image last deployed - "
                        "redeploy after changing stratum"))
    longest = max(rom["longest_item_seconds"].values(), default=0.0) * LAMBDA_SLOWDOWN
    level: Level = ("fail" if longest > timeout else
                    "warn" if longest * ROM_HIGH > timeout else "ok")
    checks.append(Check("timeout", level,
                        f"longest estimated item ~{longest:.0f} s on Lambda "
                        f"(x{ROM_HIGH:g}: {longest * ROM_HIGH:.0f} s) against {timeout} s"))
    try:
        reserved = lam.get_function_concurrency(FunctionName=fn).get(
            "ReservedConcurrentExecutions")
    except Exception:  # noqa: BLE001 - permission to read it is not required to run
        reserved = None
    inflight = concurrency(workers)
    if reserved is not None:
        checks.append(Check("concurrency", "ok" if inflight <= reserved else "warn",
                            f"{inflight} in flight against {reserved} reserved"
                            + ("" if inflight <= reserved else
                               "; the excess is throttled and retried")))
    h = rom["cpu_hours"]
    cost = lambda_cost(h, sum(rom["items"].values()), memory or 4096)
    usd = cost["usd"]
    width = max(1, min(inflight, reserved or inflight))
    hours = wall_hours(rom, width, slowdown=LAMBDA_SLOWDOWN)
    checks.append(Check("cost", "ok",
                        f"~${usd[1]:,.2f} (${usd[0]:,.2f} - ${usd[2]:,.2f}) at {memory} MB; "
                        f"~{fmt_hours(hours)} wall at {width} in flight"))
    return checks


def _boto(service: str) -> Any:
    import boto3

    return boto3.client(service)


def _code(e: Exception) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, Mapping):
        code = (resp.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(e).__name__


__all__ = ["Check", "failed", "preflight", "render", "wall_hours"]
