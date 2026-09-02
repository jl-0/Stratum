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
| `aws` | Step Functions → Lambda and Batch | Continental/global runs, bursty campaigns |

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

`stratum/executors/local.py` is the only executor in the first slice; `slurm` and `aws` are
refused by name with this section cited.

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
a hit, so a fully cached local re-run is ~11 s on the trial tile
([notes/heritage.md](../notes/heritage.md), "First measurements").

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
5. The router in §2 needs per-stage constants, and the budget's `max_vcpu_hours` is reported as
   "not estimated" until they exist. The first measurements are in
   [notes/heritage.md](../notes/heritage.md): regrid scales with a granule's coverage of the tile
   (3–19 s per granule single-threaded), resolve and reduce are ~0.5 s per 720 × 720 block with up
   to 14 candidates, publish ~1.6 s per tile. Turning those into an estimate is the next step.
6. The local executor's regrid items run the KD-tree query single-threaded inside a pool that
   already fills the node. A SLURM task should set `n_workers` from `$SLURM_CPUS_PER_TASK`
   ([03 §3](03-regrid-glt.md)); the local pool may want fewer, fatter regrid workers.
