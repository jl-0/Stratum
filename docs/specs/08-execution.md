# 08 — Execution and Orchestration

**Status:** draft · **Depends on:** [00](00-overview.md), [01](01-grid-tiling.md), [06](06-caching.md) ·
**Depended on by:** [09](09-run-manifest.md)

Where work runs, how it fans out, and what happens when it fails. Three executors — local, SLURM,
AWS — over one work-list interface.

---

## 1. Executors

Stratum is a Python package with a CLI. **The whole pipeline runs on one machine**; the other
backends exist because some runs are too large for one machine, not because the pipeline depends on
them.

| Executor | Dispatch | For |
|---|---|---|
| `local` | Process pool. **Default.** | Development, a zone, anything being debugged |
| `slurm` | Job array over a shared filesystem | An existing institutional allocation |
| `aws` | Lambda, fanned out inline. Step Functions and Batch when a run outgrows that (§2-3) | Continental/global runs, bursty campaigns |

The executor is a **CLI flag or deployment profile, never a manifest field**. Where a run executes
is a platform decision; the same manifest must give the same result on a laptop and on a 10,000-way
fan-out, which a manifest naming its own executor could not promise. Same boundary as
[ADR-0002](../decisions/ADR-0002-terraform-manifest-boundary.md).

### The portability seam

The plan stage emits a **work list**: a flat file of independent items, each naming a (granule, tile) or a
(tile, epoch, block). Every executor does the same thing with it — run each item somewhere. Items
never communicate, and each writes to a content-addressed key.

```
plan  ──▶  work list  ──▶  [ executor ]  ──▶  content-addressed artifacts
```

Consequences worth stating:

- A run interrupted locally can be finished on a cluster when both point at one cache root;
  completed items are cache hits.
- `stratum exec --plan … --stage … --index N` is the single worker entrypoint. A SLURM array task
  and a Lambda invocation both reduce to it, which is what keeps the three paths from diverging.
- Storage is a URI or a path. `{root}/cache/...` is a directory or an `s3://` prefix; layout and
  keys are identical. `{root}` is `outputs.bucket`; locally that is a path relative to the
  manifest, and `cache/`, `runs/{run_id}/` and `products/{run_id}/` hang off it exactly as they
  would in a bucket ([06 §4](06-caching.md)).

### The `local` executor as built

`stratum/executors/local.py`; `slurm` is refused by name with this section cited. The `aws`
executor is below.

| Property | Contract |
|---|---|
| Unit of work | `exec_item(run_dir, stage, index)` — the same function `stratum exec` calls; the plan is loaded once per process |
| Pool | `--workers 1` runs every item in-process (debuggable); more uses a **spawn**-context `ProcessPoolExecutor`, because netCDF4/HDF5 are not fork-safe and a reader context is not picklable |
| Outcomes | `work/{stage}.results.jsonl`, one line per item: `index`, `ok`, the key or the error plus traceback, `hit`, `seconds` — written whether or not the stage succeeded, so `stratum status` can say what happened |
| Failure | All-or-nothing: any failed item raises after the stage completes, listing the failures. §4's tolerated percentage is a later slice |
| Order | regrid → resolve → reduce → publish, then Finalize: the STAC collection, `provenance.json` ([10 §2](10-provenance.md)) and an execution section appended to `report.md` |
| Budget gate | Runs before the first stage ([09 §4](09-run-manifest.md)): over budget refuses unless `on_exceed: warn`; `require_approval` refuses too, naming `stratum approve` as the later slice (§3) |
| Exit codes | `0` ok; `1` invalid or failed; `2` valid but over budget — from both `plan` and `run` |

Every handler recomputes the content-addressed keys of its inputs exactly as the producing stage
did, so a missing input is a clear error and a present one is a hit whoever wrote it; a re-run of
a finished run reports every regrid, resolve and reduce item as a hit and rewrites publish, which
is run-prefixed and never a hit. A spawn pool costs about 2.5 s per stage even when every item is
a hit, so a fully cached local re-run is ~11 s on the trial tile.

### SLURM

A work list *is* a job array. Stages become dependent array jobs.

```bash
stratum plan -m manifest.yaml --out $SCRATCH/run-01

sbatch --array=0-724%64 --cpus-per-task=4 --mem=16G --time=00:30:00 \
       --wrap "stratum exec --plan $SCRATCH/run-01 --stage regrid --index \$SLURM_ARRAY_TASK_ID"
```

| Concern | Mapping |
|---|---|
| Fan-out | `--array=0-N`, throttled with `%K` |
| Stage ordering | `--dependency=afterok:<jobid>` |
| Shared state | Cache directory on the shared filesystem; content addressing means concurrent tasks cannot collide |
| Retries | `--requeue`; a retried item recomputes to the same key |
| Partial failure | Failed array indices are individually re-submittable — the work-list index *is* the identifier |

**Size for a block, not a tile.** Existing cluster jobs ask for 32 GB and long walltimes because they
materialize a whole tile; a 512×512 block is ~1 GB, so short, small, high-concurrency array tasks are
the better shape and fail more cheaply. Set `--cpus-per-task` to match what the regrid actually uses
— the KD-tree query is compute-bound and takes a worker count.

Credential expiry (~1 h) argues for short array tasks here just as it argues for Lambda on AWS.

### The `aws` executor as built (2026-09-14)

An **inline fan-out**: a thread pool over `lambda:InvokeFunction`, dispatched by whatever ran
`stratum run` — a laptop. Not a state machine. `run_all` takes the stage runner as a parameter,
so the stages, their order, the outcome records and Finalize are literally the same code on both
executors; only the dispatch differs.

| Property | Contract |
|---|---|
| Unit of work | An invocation carrying `{root, run_id, stage, index}` — four scalars, no geometry, well under the 256 KiB of §3 |
| Worker | `stratum/executors/awslambda.py`: load the credential, mirror the run directory if this container has not seen it, `exec_item` |
| Pool | `--workers`, else `$STRATUM_LAMBDA_CONCURRENCY`, else 32. The ceiling that binds is the function's reserved concurrency, which is a deployment setting |
| Outcomes | Identical records to `local`, including a handler exception, so `stratum status --failed` parses one format |
| Failure | All-or-nothing, as `local`. §4's tolerated percentage is still a later slice |
| Retry | None in the client (`max_attempts: 0`): an item is idempotent, so a retry is safe but never automatic, and the outcome record decides |
| Refusal | A **local storage root** is refused by name before the first invocation. A Lambda cannot read a laptop's filesystem |
| Finalize | Mirrors the products back before writing the STAC collection — the worker that published them was elsewhere |

Why a pool and not Step Functions: a Distributed Map earns its keep at item counts beyond a pool,
and costs a state machine and several more roles to deploy. The work lists are already the S3
JSONL an `ItemReader` consumes, so §3 remains additive rather than superseded.

The orchestrator holds **no state**: every result is in the bucket before the invocation returns.
Losing the laptop loses the dispatch, never the work.

### Preflight: the executor's half of validation (2026-10-07)

A plan proves the manifest and the data ([09 §5](09-run-manifest.md), [12 §7](12-data-access.md))
and is deliberately executor-agnostic, because one plan runs on several executors. Whether *this*
executor can run it is a separate question, answered after the plan and before the first item is
dispatched (`stratum/executors/preflight.py`). `stratum run` refuses on any `fail`;
`stratum plan --executor <name>` prints the same checks and exits 2. They are not written to
`plan.json`: they describe a machine or a deployment at one moment, not the run.

| Executor | Check | Fails when |
|---|---|---|
| `local` | storage root's volume | the estimated bytes written plus the eviction reserve exceed free space — a local root is the record and is never evicted. A bucket root's mirror is only reported: it is evicted |
| `local` | asset cache's volume | never; it warns when the downloads exceed free space, since LRU eviction then re-downloads |
| `local` | wall time | never; reports the estimate at the pool width |
| `aws` | credentials | `sts:GetCallerIdentity` fails — an expired session token is the common case, and it used to surface as the first upload failing after a full plan |
| `aws` | worker function | `$STRATUM_LAMBDA_FUNCTION` is unset or `GetFunctionConfiguration` fails |
| `aws` | timeout | the longest estimated item × Lambda's slowdown exceeds the function's timeout; warns at ×2 |
| `aws` | concurrency | never; warns when `--workers` exceeds reserved concurrency |
| `aws` | cost | never; the estimate re-priced at the function's real memory, and wall time at the real width |

What preflight cannot see: **which image the function runs.** It reports the function's
`LastModified`, but nothing records which stratum version an image digest carries, so a worker
older than the planner — one that predates a field the plan relies on, such as `table_probes` —
is not detected (question 8).

---

## 2. AWS split plane

Serverless is right for the two cheap stages and wrong for the three that carry the data. The
honest answer is a split, not a choice.

| Plane | Service | Stages | Why |
|---|---|---|---|
| **Control** | Step Functions + Lambda | plan, publish + all cache probes | Seconds, hundreds of MB, thousands of invocations. Serverless genuinely pays here. |
| **Data** | AWS Batch on EC2 Spot | regrid, resolve, reduce when large | Minutes–hours, GBs of RAM, GDAL in the image. Spot with block-level retries. |
| **Either** | routed per work item | regrid, resolve, reduce when small | Blocks make Lambda viable where tiles never were |

Lambda's ceilings are hard: **15 minutes, 10 GB memory, 10 GB `/tmp`**. AMD asks Slurm for 32 GB
and up to 24 h per 1° bin. Block decomposition ([01](01-grid-tiling.md)) shrinks the unit until
Lambda fits — which is why the answer is a router rather than a verdict.

**As built (2026-09-14): one plane, no router, no Batch.** Every work item of the reference run
clears all three ceilings by one to three orders of magnitude — the slowest measured item is a
regrid of a granule covering the whole tile at about 19 s single-threaded, against a 900 s
timeout; a 720 × 720 block is about 1 GB against 10 GB of memory and 10 GB of `/tmp`. So there is
no placement decision to make and nothing to provision. The router below is what this becomes
when an item appears that does not fit, not a stage that was skipped: the estimates it needs are
open question 5.

The plan stage runs wherever `stratum run` runs, which today is a laptop. Routing it, and moving
the orchestrator off the laptop, are the third and fourth steps of the deployment.

### One image, two entrypoints

Zip layers cap at 250 MB unzipped across all layers; GDAL + netCDF4 + scipy + numpy exceeds that
before any of our code. **Container image Lambdas allow 10 GB and are the only viable path.** The
same image carries a Lambda Runtime Interface Client entrypoint and a plain CLI entrypoint, so
identical code runs on Lambda, on Batch, on the cluster, and on a laptop.

### Routing

```python
def route(item, plan) -> Literal["lambda", "batch"]:
    if plan.capability == "tile":       return "batch"   # needs a global view
    if item.est_peak_bytes > 7 * GB:    return "batch"
    if item.est_seconds    > 600:       return "batch"   # 15-min ceiling, with margin
    if item.est_bytes_read > 8 * GB:    return "batch"
    return "lambda"
```

Estimates come from the planner: block cell count × bands × dtype × observation count, plus
measured per-stage constants refined from prior runs.

The plan itself is routed the same way, by the tile and epoch counts the manifest implies before a
single block is enumerated. It writes per-tile work lists as it goes, so a run near the threshold
checkpoints rather than fails, and a local run is unaffected either way.


### The run estimate (2026-10-07)

Every plan carries a rough order of magnitude (`stratum/plan/estimate.py`, `plan.json`
`estimate`, the report's *Estimate* section): CPU time per stage, the longest item per stage,
the Lambda bill at the deployment's default memory, unique download bytes, and the bytes the run
will write. It is computed from the finished work lists, because each item's cost depends on
what it touches:

| Stage | Model, seconds on the calibration core | Fitted r |
|---|---|---|
| regrid | 6.56 + 23.44 × (tile cells the granule's bbox covers ÷ 12.96 M) | 0.78 |
| resolve | 0.074 + 0.080 × candidates × (block cells × bytes per cell ÷ 5.18 M) | 0.92 |
| reduce | 8.65 × (block cells × bytes per cell ÷ 5.18 M) | flat in epochs (−0.17) |
| publish | 3.87 × (tile cells × bytes per cell ÷ 129.6 M) | one item |

Bytes per cell is each snapshot layer's dtype size × its band count. Each resolve item records
its candidate count (`candidates`, for the estimate only; a worker ignores it). The constants are
fitted to one cold run with every item recorded — `examples/emit-cmr-nevada`, run
`nevada-regrid-cost-probe-20260917-d095b352`, 33 granules, one 1° tile at 1″, `block_size: 720`,
three layers at 10 bytes a cell, Apple M-series — and the report names it. Lambda is priced at
2.25× the calibration core's time (the measured 2–2.5×), arm64 GB-seconds and requests; the
footprint uses the same run's cache measured by kind, plus 23 MB per ortho role per regrid item.

The range is ×0.5 to ×2. [observed] Out of sample, on the US West 2025 plan (8 layers, 34 bytes a
cell, an ortho role and aux rasters the calibration run lacked): **126 CPU-hours, $55 on Lambda**
against the README's independent hand estimate of 91–234 CPU-hours and $35–112. The total
agrees; the split does not — resolve comes out at 8.5 h against 19–127 h, because nothing prices
the ortho warp or aux reads, and reduce at 81 h against 24–59 h, because bytes per cell
over-counts an eight-layer schema. The two errors partly cancel (§7, question 7).

`budget.max_vcpu_hours` is checked against the central estimate after the work lists exist — a
second gate, since the first runs before any granule is opened ([09 §4](09-run-manifest.md)). An
over-budget plan is still written, work lists and all, and `run_all` refuses it.

---

## 3. State machine

```
Plan                # Lambda: manifest -> frozen index -> work list -> S3
BudgetGate          # Choice: within budget? else -> ApprovalWait (task token)
RegridMap           # Distributed Map over (granule x tile), ItemReader = S3 JSONL
  └─ CacheProbe -> Regrid          # skipped entirely on hit
EpochMap            # Distributed Map over epochs
  └─ BlockMap       # nested Distributed Map over (tile x block)
       └─ Resolve
ReduceMap           # Distributed Map over (tile x block)
PublishMap          # Distributed Map over tiles: stitch, COG, render, STAC
Finalize            # provenance record, run report, notification
```

**Distributed Map** is the feature that makes this shape work: it reads its work list from an S3
object rather than from state, and fans out to 10,000 concurrent child executions. Nested maps
(tiles → blocks) keep payloads small. **Variables + JSONata** carry the manifest hash and cache
prefix across states without a chain of Pass states.

### Constraints to design around

| Constraint | Consequence |
|---|---|
| `ItemReader`/`ResultWriter` need buckets in the **same account and region** as the state machine | Pins the deployment to `us-west-2`, where the data lives |
| Child execution input capped at **256 KiB** | Work items carry keys and hashes, never geometry or band lists |
| Standard workflows bill per state transition | Use **Express** child workflows on inner maps — at 100k+ blocks this is a real line item |
| Max concurrency 10,000 | A CONUS run at 1°/512px is ~44k blocks: nested maps, or accept waves |

---

## 4. Failure handling

Neither existing pipeline retries anything. `watch.sh` infers a job id by regexing a log filename,
counts finished bins, and greps stderr for `error|fail|oom` — so any library warning containing
"error" reads as failure, and an OOM-killed task that wrote no stderr reads as success. It is also
broken as written: it matches `*Finished*` against `tail -n 1`, but `pipeline.sh`'s last line is
`Done`. A failed 1° bin stays failed until a human notices.

We do better by construction, not by adding a supervisor:

1. **Retry with backoff** on transient failures (Spot reclaim, throttling, credential expiry).
2. **Content addressing makes retry safe** — a retried block recomputes to the same key.
3. **Atomic writes** so an interrupted task cannot leave a truncated artifact that later reads as
   a hit.
4. **`ResultWriter` captures per-item outcomes**, so failures are a queryable list rather than a
   log grep.
5. **`ToleratedFailurePercentage`** lets a run complete with a handful of bad blocks and report
   them, instead of failing 44,000 items because one granule is corrupt.
6. **Blast radius is one block**, which is what makes Spot economically sensible.

---

## 5. Credentials

EMIT data lives in `lp-prod-protected` (us-west-2) behind Earthdata Login, and **S3 credentials
are temporary — roughly one hour.**

AMD's 24-hour Slurm jobs would simply fail partway through if ported naively. Requirements:

- EDL credential in Secrets Manager, never in the image;
- a refresh wrapper that re-fetches before expiry, wrapping every long read;
- Batch jobs must assume refresh is needed; Lambda's 15-minute ceiling makes it a non-issue there,
  which is a quiet argument for routing to Lambda where possible.

**In a deployment (2026-09-14).** The worker reads `$STRATUM_EDL_SECRET` — the secret's *name*,
which is the only part that is configuration — and fetches the value from Secrets Manager at run
time, once per container, setting `EARTHDATA_USERNAME`/`PASSWORD` or `EARTHDATA_TOKEN` in its own
process before the first login. Both secret shapes work and the code prefers neither. The value is
never in the function's `environment` block, because anything there is in Terraform state *and*
readable by `lambda:GetFunction`. No invocation runs past Lambda's 15-minute ceiling, so nothing on
this path refreshes a NASA credential mid-read; the refresh wrapper remains unbuilt and unneeded
here. `$STRATUM_EDL_SECRET` unset means do nothing — a run over public assets, and every test.

**As built (2026-09-02, HTTPS only).** `stratum/access/auth.py` is the one place a credential is
read: `earthdata_login()` tries `~/.netrc` (or `$NETRC`) for `urs.earthdata.nasa.gov`, then
`EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`, building its own `earthaccess.Auth` rather than
touching module state; the result is cached per process, never obtained at import, and never
on a local-only run or an index build (a CMR search is anonymous). A spawned worker logs in
afresh on its first remote asset. The `AssetStore` re-logins **once** on a `401`/`403` and
retries the request; a second refusal is an `AssetFetchError` with the URL and both statuses.
Nothing logs, stores or raises a token or password — `EarthdataLoginError` names strategies and
exception types only, and no credential reaches `plan.json`, the report or provenance. Secrets
Manager is built (above). The S3 credential exchange for `lp-prod-protected` and the refresh
wrapper are not: a granule is read over HTTPS from Lambda as from a laptop ([12 §4](12-data-access.md)).

---

## 6. Multiple runs, one deployment

Running five competing cost functions is a **manifest concern, not an infrastructure concern**.
Terraform stands up one run-agnostic deployment; a run is a manifest plus a `run_id` that becomes
an S3 prefix and a Step Functions execution.

Because GLTs carry no dependence on the scorer, **every experiment over the same zone shares one
GLT cache** — the first pays for geometry, the rest skip plan and regrid. Comparing cost functions is
the cheapest thing the system does.

Guardrails: tag every job with `run_id` for cost allocation; set `MaxConcurrency` per execution so
one run cannot starve another; give outputs a `run_id` prefix while the cache stays shared. Reach
for a second deployment only on a real boundary — a separate account for delivered products
versus experimentation, or a different region — never for an experiment.

---

## 7. Open questions

1. Batch on Fargate or EC2? Fargate is simpler and now supports Graviton Spot; EC2 gives better
   instance selection for memory-heavy reduces and local NVMe for staging.
2. ~~Do we need a priority queue so a delivered-product run preempts experiments?~~ **Resolved:**
   no, not until two campaigns actually contend for one deployment.
3. ~~Should `Plan` itself be a Batch job for global runs?~~ **Resolved:** routed like any other work
   item, by the tile and epoch counts the manifest implies, with per-tile work lists written as it
   goes (§2).
4. ~~Is `ToleratedFailurePercentage` acceptable for a delivered product, or must delivery runs be
   all-or-nothing?~~ **Resolved:** tolerate in experiments; delivery runs are all-or-nothing.
5. ~~The router in §2 needs per-stage constants, and the budget's `max_vcpu_hours` is reported
   as "not estimated" until they exist.~~ **Estimate built 2026-10-07; the router is not.** §2, *The run estimate*,
   has the model. The router can now be written against `longest_item_seconds`, and is not
   needed until an item approaches Lambda's ceilings.
6. The local executor's regrid items run the KD-tree query single-threaded inside a pool that
   already fills the node. A SLURM task should set `n_workers` from `$SLURM_CPUS_PER_TASK`
   ([03 §3](03-regrid-glt.md)); the local pool may want fewer, fatter regrid workers.
7. The estimate prices cells and bytes, not the work a role or plugin adds: an ortho warp, aux
   reads, masks. On US West that under-states resolve ~2–15× and over-states reduce ~1.5×. A term
   per ortho role and per aux source, fitted to a run that has them, and calibration from each
   finished run's outcome records (they carry seconds per item) would tighten it.
8. Nothing records which stratum version a deployed image carries, so preflight cannot tell a
   worker older than the planner from a current one. A version label on the image, read back
   through `GetFunctionConfiguration`, would make it a check.
