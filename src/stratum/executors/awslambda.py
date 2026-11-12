"""The Lambda worker: one work item per invocation (08 sections 1-2).

`handler` is the function the Lambda runtime calls. It does three things and nothing else:

1. puts the Earthdata credential into this process's environment, once per container;
2. reconstitutes the workspace from the event's `root` and pulls the run directory into
   `/tmp` if this container has not seen it;
3. calls `exec_item` - the same function `stratum exec`, the local pool and a SLURM array task
   call, which is what keeps the four paths from diverging.

The event is five scalars, because a Step Functions child execution input is capped at 256 KiB
and a work item must never carry geometry:

    {"root": "s3://bucket/prefix/", "run_id": "cm-20260914-a1b2c3d4", "stage": "regrid",
     "index": 41, "plan": "sha256:..."}

`plan` is the digest of the plan.json the dispatcher is running (`plan_digest`). A run id does not
identify a plan - re-planning one manifest the same day over a rebuilt index reuses it - so a
warm container compares the digest with the plan it holds and starts over when they differ.

**No credential is ever configured into the function.** A value in Terraform's `environment`
block lands in Terraform state and in the function configuration, where `lambda:GetFunction` can
read it; the secret is fetched here at run time instead, and only its *name* is configuration.
Nothing in this module logs, returns or raises a credential.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from stratum.executors.worker import clear_cache, exec_item
from stratum.plan.document import plan_digest
from stratum.storage import Workspace

log = logging.getLogger(__name__)

SECRET_ENV = "STRATUM_EDL_SECRET"
# Which JSON keys of the secret map onto which variables the credential resolver reads
# (`stratum.access.auth`). Both shapes are supported; neither is preferred by the code.
SECRET_KEYS = {"username": "EARTHDATA_USERNAME", "password": "EARTHDATA_PASSWORD",
               "token": "EARTHDATA_TOKEN"}

_credentials_loaded = False


class HandlerError(RuntimeError):
    """A malformed event, or a secret that cannot be read. Never carries a secret value."""


# ------------------------------------------------------------------------------- credentials
def load_credentials(*, force: bool = False) -> list[str]:
    """Copy the Earthdata secret into the environment. Returns the variable NAMES it set, which
    is all a log line may ever say about it.

    A container is reused across invocations, so this runs once unless forced. When
    `$STRATUM_EDL_SECRET` is unset nothing happens at all - a run over public assets, or a test.
    """
    global _credentials_loaded
    if _credentials_loaded and not force:
        return []
    name = os.environ.get(SECRET_ENV)
    if not name:
        _credentials_loaded = True
        return []
    import boto3

    try:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=name)["SecretString"]
        doc = json.loads(raw)
    except Exception as e:  # noqa: BLE001 - the type is reported, never the value
        raise HandlerError(f"could not read the Earthdata secret {name!r}: "
                           f"{type(e).__name__}") from None
    if not isinstance(doc, dict):
        raise HandlerError(f"the Earthdata secret {name!r} is not a JSON object; it must be "
                           '{"username": ..., "password": ...} or {"token": ...}')
    unknown = sorted(set(doc) - set(SECRET_KEYS))
    if unknown:
        raise HandlerError(f"the Earthdata secret {name!r} has unexpected key(s) {unknown}; "
                           f"expected any of {sorted(SECRET_KEYS)}")
    set_names = []
    for key, var in SECRET_KEYS.items():
        if doc.get(key):
            os.environ[var] = str(doc[key])
            set_names.append(var)
    if not set_names:
        raise HandlerError(f"the Earthdata secret {name!r} is empty")
    _credentials_loaded = True
    log.info("Earthdata credential loaded from Secrets Manager into %s", ", ".join(set_names))
    return set_names


def reset_credentials() -> None:
    """Forget that the secret was loaded (tests, and a forced refresh)."""
    global _credentials_loaded
    _credentials_loaded = False


# ----------------------------------------------------------------------------------- the run
def run_dir_for(root: str, run_id: str, plan: str | None = None) -> Any:
    """The local run directory for `{root}/runs/{run_id}`, pulled from the bucket if this
    container has not seen it. A warm container skips the pull - provided the plan it holds is
    the one being run.

    `plan` is the dispatcher's `plan_digest`. When this container's copy differs, the run was
    re-planned under the same id since the container last pulled it: the mirrored run directory
    is dropped and pulled again (`pull_tree` leaves an existing file alone, so the stale plan and
    work lists would otherwise survive), and the `RunPlan` memoised in this process goes with it.
    If the bucket's plan is still not the dispatcher's, the dispatcher is the stale one, and the
    item is refused rather than run from a work list it was not numbered against. An event with
    no `plan` is taken as it always was."""
    ws = Workspace.for_root(root)
    run_dir = ws.path / "runs" / run_id
    plan_path = run_dir / "plan.json"
    if plan is not None and plan_path.is_file() and plan_digest(run_dir) != plan:
        log.info("run %s was re-planned; dropping this container's copy", run_id)
        ws.drop_mirror(run_dir)
        clear_cache()
    if not plan_path.is_file():
        ws.pull_tree(run_dir)
    if not plan_path.is_file():
        raise HandlerError(f"no plan.json under {ws.url(run_dir)}; run `stratum plan` first")
    if plan is not None and plan_digest(run_dir) != plan:
        raise HandlerError(f"{ws.url(run_dir)} holds another plan than the one this item was "
                           "dispatched from: the run was re-planned while it was executing. "
                           "Start it again from the re-plan")
    return run_dir


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """The Lambda entrypoint. Returns the same outcome record every executor writes to
    `work/{stage}.results.jsonl`, so the dispatcher needs no Lambda-specific parsing."""
    missing = [k for k in ("root", "run_id", "stage", "index") if k not in event]
    if missing:
        raise HandlerError(f"event lacks {missing}; expected root, run_id, stage, index")
    load_credentials()
    plan = event.get("plan")
    run_dir = run_dir_for(str(event["root"]), str(event["run_id"]),
                          str(plan) if plan is not None else None)
    return exec_item(run_dir, str(event["stage"]), int(event["index"]))


__all__ = ["SECRET_ENV", "SECRET_KEYS", "HandlerError", "handler", "load_credentials",
           "reset_credentials", "run_dir_for"]
