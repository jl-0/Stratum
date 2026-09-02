"""The plan stage: resolve, freeze, work list, budget gate, validation (00 section 2 stage 1,
09 sections 4-5).

    result = plan_run("manifest.yaml")          # writes {root}/runs/{run_id}/...
    run = load_run(result.run_dir)               # what a worker rebuilds from plan.json
    read_work(result.run_dir, "regrid")          # the stage's items

`build_index_from_manifest` is `stratum index build`; `plan_run` calls it implicitly for a
local source whose index file does not exist yet, so a laptop run is one command.
"""
from __future__ import annotations

from stratum.plan.document import (
    MERGED_NAME,
    PLAN_NAME,
    PLAN_SCHEMA_VERSION,
    REPORT_NAME,
    STAGES,
    WORK_DIR,
    BudgetExceeded,
    PlanError,
    RunPlan,
    context_from_doc,
    context_to_doc,
    load_run,
    read_plan,
    read_results,
    read_work,
    results_path,
    schema_from_doc,
    schema_to_doc,
    work_path,
    write_plan,
    write_work,
)
from stratum.plan.run import (
    Inspection,
    PlanResult,
    build_index_from_manifest,
    build_work_lists,
    index_path,
    inspect_granules,
    plan_run,
    render_report,
    resolve_local,
    roles_needed,
    storage_root,
)

__all__ = [
    "MERGED_NAME", "PLAN_NAME", "PLAN_SCHEMA_VERSION", "REPORT_NAME", "STAGES", "WORK_DIR",
    "BudgetExceeded", "Inspection", "PlanError", "PlanResult", "RunPlan",
    "build_index_from_manifest", "build_work_lists", "context_from_doc", "context_to_doc",
    "index_path", "inspect_granules", "load_run", "plan_run", "read_plan", "read_results",
    "read_work", "render_report", "resolve_local", "results_path", "roles_needed",
    "schema_from_doc", "schema_to_doc", "storage_root", "work_path", "write_plan", "write_work",
]
