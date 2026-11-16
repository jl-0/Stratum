"""The `aws` executor: an inline fan-out over Lambda (08 sections 1-2).

The same shape as `executors/local.py` - a pool over the work list, one outcome record per item,
all-or-nothing - with `lambda:InvokeFunction` where the process pool would be. The orchestrator
is whatever ran `stratum run`: a laptop, or later a task. Nothing is provisioned to run it.

Why a thread pool and not Step Functions: a Distributed Map earns its keep at item counts beyond
a pool, and it costs three more IAM roles and a state machine to deploy. The work lists are
already the S3 JSONL an `ItemReader` consumes, so that remains additive
([08 §3](../../docs/specs/08-execution.md)).

Two properties make this safe to run at width:

- **an item is idempotent**, because it writes to a content-addressed key, so a retried
  invocation recomputes the same bytes or finds a hit;
- **the orchestrator holds no state**, because every result is in the bucket before the
  invocation returns. Losing the laptop loses the dispatch, never the work.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from stratum import console
from stratum.executors.worker import load_cached
from stratum.plan.document import plan_digest, read_work

log = logging.getLogger(__name__)

FUNCTION_ENV = "STRATUM_LAMBDA_FUNCTION"
CONCURRENCY_ENV = "STRATUM_LAMBDA_CONCURRENCY"
DEFAULT_CONCURRENCY = 32
# A worker may run for the whole of Lambda's ceiling, so the client must not give up first, and
# an invocation is never retried by botocore: a duplicate invocation is harmless but wasteful,
# and the dispatcher's own outcome record is what decides success.
READ_TIMEOUT = 960
#: Lambda's answer to an invocation over the function's concurrency (HTTP 429). The invocation
#: never STARTED, so sending it again is not a duplicate and is the one retry the dispatcher
#: makes itself; botocore still retries nothing.
THROTTLE_CODES = frozenset({"TooManyRequestsException"})
#: Backoff between throttled attempts: doubling from 1 s, capped, each wait drawn from the
#: upper half of its step so threads throttled together do not retry together. And how long one item keeps
#: trying before it is reported as a failure. The retry rides out a brief throttle - a burst
#: limit, another consumer of the account's concurrency - and NOT a pool wider than the
#: reservation: there a thread whose invocation returns re-takes the slot with its next item and
#: the excess threads starve to this deadline. `run_stage` caps the pool so that cannot happen.
THROTTLE_BACKOFF_CAP = 30.0
THROTTLE_DEADLINE = 900.0


class AwsExecutorError(RuntimeError):
    """The deployment is not configured, or an invocation failed in a way retrying will not fix."""


def function_name(name: str | None = None) -> str:
    """The worker function: the argument, else `$STRATUM_LAMBDA_FUNCTION`. `make tf-output`
    prints it; it is deployment configuration and never a manifest field (08 section 1)."""
    resolved = name or os.environ.get(FUNCTION_ENV)
    if not resolved:
        raise AwsExecutorError(
            f"no worker function: set ${FUNCTION_ENV} to the deployment's Lambda "
            "(`make tf-output` prints it) or pass --function")
    return resolved


def concurrency(workers: int | None = None) -> int:
    """How many invocations are in flight. `--workers`, else `$STRATUM_LAMBDA_CONCURRENCY`,
    else 32. The ceiling that matters is the function's reserved concurrency, which is a
    deployment setting, so this is only how fast the dispatcher offers work."""
    if workers:
        return max(1, int(workers))
    return max(1, int(os.environ.get(CONCURRENCY_ENV) or DEFAULT_CONCURRENCY))


def reserved_concurrency(client: Any, function: str) -> int | None:
    """The function's reserved concurrency, or None when it has none or it cannot be read
    (permission to read it is not required to run)."""
    try:
        reserved = client.get_function_concurrency(FunctionName=function).get(
            "ReservedConcurrentExecutions")
    except Exception:  # noqa: BLE001
        return None
    return int(reserved) if reserved else None


def pool_ceiling(client: Any, function: str) -> int | None:
    """The most invocations of `function` that can be in flight, as far as `client` can read
    it: its reserved concurrency; without one, the account's unreserved concurrency, which the
    function shares with every other unreserved function - an upper bound, not a guarantee, so
    a throttle is still possible and the jittered retry is what absorbs it. None, with a
    warning, when neither can be read: the pool is then whatever the user asked for."""
    reserved = reserved_concurrency(client, function)
    if reserved is not None:
        return reserved
    try:
        unreserved = int(client.get_account_settings()["AccountLimit"]
                         ["UnreservedConcurrentExecutions"])
    except Exception:  # noqa: BLE001 - permission to read it is not required to run
        unreserved = 0
    if unreserved > 0:
        log.warning("%s has no reserved concurrency that can be read; it shares the account's "
                    "%d unreserved executions with every other function, so the pool is capped "
                    "there and may still be throttled. Reserve concurrency for a ceiling the "
                    "run can count on", function, unreserved)
        return unreserved
    log.warning("%s has no reserved concurrency that can be read, and neither can the "
                "account's; the pool is not capped. If --workers exceeds what the function "
                "can run, the excess is throttled until it fails after %.0f s", function,
                THROTTLE_DEADLINE)
    return None


def _client(client: Any | None = None) -> Any:
    if client is not None:
        return client
    import boto3
    from botocore.config import Config

    return boto3.client("lambda", config=Config(read_timeout=READ_TIMEOUT, connect_timeout=15,
                                                retries={"max_attempts": 0}))


def _is_throttle(e: Exception) -> bool:
    response = getattr(e, "response", None)
    error = (response.get("Error") or {}) if isinstance(response, dict) else {}
    return str(error.get("Code") or "") in THROTTLE_CODES


def _invoke_unthrottled(client: Any, **kwargs: Any) -> dict[str, Any]:
    """`client.invoke`, waiting out a throttle: the invocation never started, and the client is
    built with no retries, so without this one 429 failed the stage outright. Any other error
    is raised at once, as before."""
    waited, delay = 0.0, 1.0
    while True:
        try:
            return client.invoke(**kwargs)
        except Exception as e:
            if not _is_throttle(e) or waited >= THROTTLE_DEADLINE:
                raise
        # jittered: threads throttled at the same instant would otherwise wake at the same
        # instants, 1, 2, 4 ... s later, and collide on every attempt
        nap = delay * random.uniform(0.5, 1.0)
        time.sleep(nap)
        waited += nap
        delay = min(delay * 2, THROTTLE_BACKOFF_CAP)


def invoke(client: Any, function: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One synchronous invocation. A handler exception comes back as `FunctionError` with the
    traceback in the payload, which becomes the outcome record's `error` - the same shape a
    local worker's failure takes, so `stratum status --failed` reads both."""
    response = _invoke_unthrottled(client, FunctionName=function,
                                   InvocationType="RequestResponse",
                                   Payload=json.dumps(payload).encode())
    body = response["Payload"].read()
    try:
        doc = json.loads(body) if body else {}
    except json.JSONDecodeError:
        doc = {"errorMessage": body.decode(errors="replace")[:2000]}
    if response.get("FunctionError"):
        message = doc.get("errorMessage") or "the worker failed with no message"
        return {"index": payload["index"], "stage": payload["stage"], "ok": False,
                "error": f"{doc.get('errorType', 'LambdaError')}: {message}",
                "traceback": "".join(doc.get("stackTrace") or ())}
    return dict(doc)


def run_stage(run_dir: Path | str, stage: str, workers: int | None = None, *,
              function: str | None = None, client: Any | None = None) -> list[dict[str, Any]]:
    """Execute every item of `stage` in Lambda. Same contract as the local runner: writes
    `work/{stage}.results.jsonl`, raises `ExecutionError` if any item failed."""
    from stratum.executors.local import ExecutionError
    from stratum.plan.document import results_path

    run_dir = Path(run_dir).resolve()
    run = load_cached(run_dir)
    ws = run.workspace
    if not ws.remote:
        raise AwsExecutorError(
            f"the aws executor needs a bucket storage root; this run's root is {ws.uri}. "
            "A Lambda cannot read your laptop's filesystem - set outputs.bucket to an s3:// "
            "URI (06 section 4)")
    items: Sequence[Any] = read_work(run_dir, stage)
    name = function_name(function)
    cli = _client(client)
    # `plan` says WHICH plan: a run id is reused by a same-day re-plan, and a warm container
    # still holding the earlier one must not execute its work list (08 section 1)
    base = {"root": ws.uri, "run_id": run.run_id, "stage": stage, "plan": plan_digest(run_dir)}
    results: list[dict[str, Any]] = []
    if items:
        # never wider than the function will run: the excess does not queue, it starves (see
        # THROTTLE_DEADLINE) and its items fail
        width = min(concurrency(workers), pool_ceiling(cli, name) or len(items), len(items))
        with (ThreadPoolExecutor(max_workers=width) as pool,
              console.progress(stage, len(items)) as advance):
            futures = {pool.submit(invoke, cli, name, {**base, "index": i}): i
                       for i in range(len(items))}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001 - a dispatch failure is an outcome too
                    results.append({"index": i, "stage": stage, "ok": False,
                                    "error": f"{type(e).__name__}: {e}"})
                advance()
    results.sort(key=lambda r: r["index"])
    path = results_path(run_dir, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in results))
    ws.push_file(path)
    failed = [r for r in results if not r.get("ok")]
    if failed:
        shown = "\n".join(f"  [{r['index']}] {r.get('error')}" for r in failed[:10])
        more = f"\n  ... and {len(failed) - 10} more" if len(failed) > 10 else ""
        raise ExecutionError(f"stage {stage!r}: {len(failed)} of {len(items)} item(s) failed "
                             f"in {name} (see {path}):\n{shown}{more}")
    return results


__all__ = ["CONCURRENCY_ENV", "DEFAULT_CONCURRENCY", "FUNCTION_ENV", "AwsExecutorError",
           "concurrency", "function_name", "invoke", "pool_ceiling", "reserved_concurrency",
           "run_stage"]
