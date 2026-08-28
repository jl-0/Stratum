# 08 — Execution and Orchestration

**Status:** draft · **Depends on:** [00](00-overview.md), [01](01-grid-tiling.md), [06](06-caching.md) ·
**Depended on by:** [09](09-run-manifest.md)

Where work runs, how it fans out, and what happens when it fails.

---

## 1. Split plane

Serverless is right for three of the five stages and wrong for the two that matter most. The
honest answer is a split, not a choice.

| Plane | Service | Stages | Why |
|---|---|---|---|
| **Control** | Step Functions + Lambda | 1, 5 + all cache probes | Seconds, hundreds of MB, thousands of invocations. Serverless genuinely pays here. |
| **Data** | AWS Batch on EC2 Spot | 2, 3, 4 when large | Minutes–hours, GBs of RAM, GDAL in the image. Spot with block-level retries. |
| **Either** | routed per work item | 2, 3, 4 when small | Blocks make Lambda viable where tiles never were |

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

---

## 2. State machine

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

## 3. Failure handling

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

## 4. Credentials

EMIT data lives in `lp-prod-protected` (us-west-2) behind Earthdata Login, and **S3 credentials
are temporary — roughly one hour.**

AMD's 24-hour Slurm jobs would simply fail partway through if ported naively. Requirements:

- EDL credential in Secrets Manager, never in the image;
- a refresh wrapper that re-fetches before expiry, wrapping every long read;
- Batch jobs must assume refresh is needed; Lambda's 15-minute ceiling makes it a non-issue there,
  which is a quiet argument for routing to Lambda where possible.

---

## 5. Multiple runs, one deployment

Running five competing cost functions is a **manifest concern, not an infrastructure concern**.
Terraform stands up one run-agnostic deployment; a run is a manifest plus a `run_id` that becomes
an S3 prefix and a Step Functions execution.

Because GLTs carry no dependence on the scorer, **every experiment over the same zone shares one
GLT cache** — the first pays for geometry, the rest skip stages 1–2. Comparing cost functions is
the cheapest thing the system does.

Guardrails: tag every job with `run_id` for cost allocation; set `MaxConcurrency` per execution so
one run cannot starve another; give outputs a `run_id` prefix while the cache stays shared. Reach
for a second deployment only on a real boundary — a separate account for delivered products
versus experimentation, or a different region — never for an experiment.

---

## 6. Open questions

1. Batch on Fargate or EC2? Fargate is simpler and now supports Graviton Spot; EC2 gives better
   instance selection for memory-heavy reduces and local NVMe for staging.
2. Do we need a priority queue so a delivered-product run preempts experiments?
3. Should `Plan` itself be a Batch job for global runs? Enumerating 44k blocks may exceed a
   Lambda's 15 minutes.
4. Is `ToleratedFailurePercentage` acceptable for a delivered product, or must delivery runs be
   all-or-nothing? Probably: tolerate in experiments, zero-tolerance for delivery.
