"""local, slurm, aws - three dispatchers over one work list (08). `exec_item` is the worker
entrypoint every one of them calls (08 section 1), and `run_all` is the stage order and Finalize,
which are the same whoever executes the items.

`slurm` is not built; it is refused by name with its section cited.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from stratum.executors.local import ExecutionError, budget_gate, run_stage
from stratum.executors.local import run_all as _run_all
from stratum.executors.worker import exec_item, load_cached, product_key

EXECUTORS: tuple[str, ...] = ("local", "aws")


def executor_suits_root(manifest_path: str, executor: str) -> None:
    """Refuse an executor that cannot reach this manifest's storage root, BEFORE planning.

    The pairing is one-directional: `aws` needs a bucket root because a Lambda cannot read the
    submitting machine's filesystem, while `local` runs against either. That asymmetry is why the
    executor is a flag and not a manifest field (08 section 1) - an `s3://` manifest is runnable
    both ways, and running one manifest on two executors and diffing the products is how the
    cloud path is validated at all.

    `aws.run_stage` checks the same thing again from the frozen plan, which is the authoritative
    check; this one exists so the refusal costs nothing. Planning a catalogue run downloads
    assets, and being told the root is wrong afterwards is a needless bill.
    """
    if executor != "aws":
        return
    from stratum.manifest import load_manifest
    from stratum.storage import parse_s3
    bucket = load_manifest(manifest_path).outputs.bucket
    if parse_s3(str(bucket)) is None:
        raise NotImplementedError(
            f"--executor aws needs a bucket storage root, but outputs.bucket is {bucket!r}. "
            "A Lambda cannot read this machine's filesystem: point outputs.bucket at the "
            "deployment's s3:// root (`make infra-output` prints it), or run with "
            "--executor local, which works against either (06 section 4, 08 section 1)")


def root_is_reachable(manifest_path: str) -> None:
    """Refuse a bucket storage root we have no AWS credentials for, BEFORE planning.

    Without this the run plans to completion - which for a catalogue source downloads assets -
    and then dies inside botocore on the first upload with `NoCredentialsError`, a message that
    names neither the bucket nor the profile it looked for. The cost of finding out is the whole
    plan stage.
    """
    from stratum.manifest import load_manifest
    from stratum.storage import parse_s3
    bucket = str(load_manifest(manifest_path).outputs.bucket)
    if parse_s3(bucket) is None:
        return
    import boto3
    if boto3.Session().get_credentials() is not None:
        return
    raise NotImplementedError(
        f"outputs.bucket is {bucket!r} but no AWS credentials were found. boto3 looks at "
        "AWS_PROFILE, AWS_ACCESS_KEY_ID, ~/.aws/credentials and the instance role, in that "
        "order. Run via ./scripts/stratum.sh, which loads .env, or export AWS_PROFILE "
        "yourself; if the profile is federated the session may simply have expired.")


def executor_available(name: str) -> None:
    """Refuse an executor this build does not ship, naming the section."""
    if name not in EXECUTORS:
        raise NotImplementedError(f"executor {name!r} is a later slice (08 sections 1-2); "
                                  f"available: {', '.join(EXECUTORS)}")


def stage_runner(executor: str = "local") -> Any:
    """How one stage's items are executed. `aws` is imported only when it is asked for, so a
    local run never imports boto3."""
    executor_available(executor)
    if executor == "aws":
        from stratum.executors.aws import run_stage as aws_run_stage

        return aws_run_stage
    return run_stage


def run_all(run_dir: Path | str, workers: int | None = None,
            executor: str = "local") -> dict[str, Any]:
    """Plan through Finalize on the named executor. The stages, their order and everything
    Finalize writes are identical either way; only the dispatch differs."""
    return _run_all(run_dir, workers, stage_runner=stage_runner(executor), executor=executor)


__all__ = ["EXECUTORS", "ExecutionError", "budget_gate", "exec_item", "executor_available",
           "executor_suits_root", "root_is_reachable",
           "load_cached", "product_key", "run_all", "run_stage", "stage_runner"]
