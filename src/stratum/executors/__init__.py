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
           "load_cached", "product_key", "run_all", "run_stage", "stage_runner"]
