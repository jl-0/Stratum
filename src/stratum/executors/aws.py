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
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from stratum.executors.worker import load_cached
from stratum.plan.document import read_work

FUNCTION_ENV = "STRATUM_LAMBDA_FUNCTION"
CONCURRENCY_ENV = "STRATUM_LAMBDA_CONCURRENCY"
DEFAULT_CONCURRENCY = 32
# A worker may run for the whole of Lambda's ceiling, so the client must not give up first, and
# an invocation is never retried by botocore: a duplicate invocation is harmless but wasteful,
# and the dispatcher's own outcome record is what decides success.
READ_TIMEOUT = 960


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


def _client(client: Any | None = None) -> Any:
    if client is not None:
        return client
    import boto3
    from botocore.config import Config

    return boto3.client("lambda", config=Config(read_timeout=READ_TIMEOUT, connect_timeout=15,
                                                retries={"max_attempts": 0}))


def invoke(client: Any, function: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One synchronous invocation. A handler exception comes back as `FunctionError` with the
    traceback in the payload, which becomes the outcome record's `error` - the same shape a
    local worker's failure takes, so `stratum status --failed` reads both."""
    response = client.invoke(FunctionName=function, InvocationType="RequestResponse",
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
    base = {"root": ws.uri, "run_id": run.run_id, "stage": stage}
    results: list[dict[str, Any]] = []
    if items:
        with ThreadPoolExecutor(max_workers=min(concurrency(workers), len(items))) as pool:
            futures = {pool.submit(invoke, cli, name, {**base, "index": i}): i
                       for i in range(len(items))}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as e:  # noqa: BLE001 - a dispatch failure is an outcome too
                    results.append({"index": i, "stage": stage, "ok": False,
                                    "error": f"{type(e).__name__}: {e}"})
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
           "concurrency", "function_name", "invoke", "run_stage"]
