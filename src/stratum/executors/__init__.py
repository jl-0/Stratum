"""local, slurm, aws - three dispatchers over one work list (08). Only `local` exists in this
slice; `exec_item` is the worker entrypoint every one of them calls (08 section 1)."""
from __future__ import annotations

from stratum.executors.local import ExecutionError, budget_gate, run_all, run_stage
from stratum.executors.worker import exec_item, load_cached, product_key

EXECUTORS: tuple[str, ...] = ("local",)


def executor_available(name: str) -> None:
    """Refuse an executor this slice does not ship, naming the section."""
    if name not in EXECUTORS:
        raise NotImplementedError(f"executor {name!r} is a later slice (08 sections 1-2); "
                                  f"available: {', '.join(EXECUTORS)}")


__all__ = ["EXECUTORS", "ExecutionError", "budget_gate", "exec_item", "executor_available",
           "load_cached", "product_key", "run_all", "run_stage"]
