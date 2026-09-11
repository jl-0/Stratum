# AWS deployment playbook

**Internal.** Written 2026-09-03, before any infrastructure exists. This is the deployment build
contract: what a human does once, what Terraform generates, what the deploy loop looks like, and
the order the pieces get built in.

[08](../specs/08-execution.md) stays authoritative for the execution architecture and
[ADR-0002](../decisions/ADR-0002-terraform-manifest-boundary.md) for the config boundary. This note
does not restate them. It records the *operational* decisions those documents leave open, so that
the Terraform and the S3 storage root can be written against something.

---

## 1. Naming and placeholders

| Placeholder | Value used throughout | Change it to |
|---|---|---|
| AWS profile | `mosaic-gen` | Whatever your federated login produces. **This name is a placeholder and is expected to be changed.** |
| Region | `us-west-2` | Nothing. Pinned by [08 §3](../specs/08-execution.md): `ItemReader`/`ResultWriter` need buckets in the same account and region as the state machine, and `lp-prod-protected` is us-west-2. |
| Deployment name | `stratum-dev` | One per deployment, not per run. A new experiment is a new manifest ([ADR-0002](../decisions/ADR-0002-terraform-manifest-boundary.md) corollary 1). |
| State bucket | `stratum-tfstate-<account-id>` | Account-unique; bucket names are global. |
| Data bucket | `stratum-<deployment>-<account-id>` | Holds `cache/`, `runs/`, `products/` — the `{root}` of [06 §4](../specs/06-caching.md). |

Every command below assumes `AWS_PROFILE=mosaic-gen` and `AWS_REGION=us-west-2` are exported.

**Federated sessions are short.** The existing profiles on this workstation are SAML-federated and
expire on the order of an hour. Re-authenticate immediately before `terraform apply`, and keep
applies small enough to finish inside a session. An apply that dies half-way on an expired token
is recoverable — the state is in S3 and locked — but it is an avoidable annoyance.

---

## 2. Preconditions — human, one-time, not automatable

Nothing in this list can be generated. Everything after it can.

| # | Precondition | Check |
|---|---|---|
| 1 | An AWS account, and a role your federated login can assume with permission to create S3, IAM, ECR, Lambda, Batch, Step Functions, Secrets Manager | `aws sts get-caller-identity` |
| 2 | A profile named `mosaic-gen` (or the real name, substituted everywhere) | `aws configure list-profiles` |
| 3 | An Earthdata Login account | Log in at `urs.earthdata.nasa.gov` |
| 4 | **The LP DAAC EULA accepted by that EDL account** | Without it, downloads fail `401` with no useful message |
| 5 | **Someone who may create IAM roles**, and knowledge of any site naming or permissions-boundary requirements. This is the §4 platform-owner persona | Site-dependent; ask before planning §4 |
| 6 | Service quotas: **Lambda concurrent executions** and Step Functions Distributed Map concurrency. Batch vCPU is not needed on this path (§8) | Defaults carry this example; a continental run needs an increase, and those take days |

Item 5 is the one that bites late. A CONUS run at 1°/512px is ~44k blocks
([08 §3](../specs/08-execution.md)); the default account concurrency will not carry it.

---

## 3. Bootstrap — before the first `terraform apply`

The Terraform root cannot create its own state backend. This is one shell script using the AWS
CLI, deliberately **not** a second Terraform root with local state to migrate.

```bash
# scripts/bootstrap-backend.sh - run once per account
BUCKET="stratum-tfstate-$(aws sts get-caller-identity --query Account --output text)"

aws s3api create-bucket --bucket "$BUCKET" --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Versioning is not optional: it is the only recovery path from a corrupted or truncated state file.

**No DynamoDB lock table.** Terraform 1.15 is installed; native S3 locking via `use_lockfile = true`
has been available since 1.10 and `dynamodb_table` is deprecated. The backend block is:

```hcl
terraform {
  backend "s3" {
    bucket       = "stratum-tfstate-<account-id>"
    key          = "stratum-dev/terraform.tfstate"
    region       = "us-west-2"
    profile      = "mosaic-gen"
    encrypt      = true
    use_lockfile = true
  }
}
```

**The state bucket is a secret store.** Terraform state contains every value Terraform touches, in
plaintext JSON. That is the whole reason §6 forbids putting the EDL credential through Terraform.
Treat this bucket's access policy with the same care as the secret itself.

---

## 4. Two Terraform roots

The split is driven by a hard constraint: **at many sites only certain people may create IAM
roles**, and role creation may carry naming and permissions-boundary requirements. Rather than
treat that as an obstacle, make it the seam.

| Root | Creates | Applied by | State key |
|---|---|---|---|
| `platform/` | IAM roles, data bucket, ECR repository, EDL secret container, log group retention | The account owner, rarely | `platform/terraform.tfstate` |
| `deployment/` | Lambda functions, ECS task definitions, budgets, alarms. **Creates no IAM** | Anyone with bucket write, often | `deployment/terraform.tfstate` |

`deployment/` consumes role ARNs from `platform/` through `terraform_remote_state`. Two state keys,
not one: the bucket prefixes can then carry different IAM, so a deployer can neither read nor
clobber platform state.

```
.env.example      committed, placeholders only;  .env is gitignored and holds everything private
Makefile          thin wrappers: bootstrap, tf-init, tf-plan, tf-apply
terraform/
  platform/       main.tf variables.tf s3.tf ecr.tf secrets.tf iam.tf outputs.tf   BUILT
  deployment/     main.tf lambda.tf ecs.tf observability.tf variables.tf           not started
  backend.hcl     GENERATED by scripts/write-backend.sh, gitignored
scripts/
  common.sh                  loads .env, maps it onto TF_VAR_*
  bootstrap-backend.sh       section 3
  write-backend.sh           generates terraform/backend.hcl from .env
  tf.sh                      terraform with .env loaded and the right backend
  put-edl-secret.sh          section 7, interactive, never logs the value       not started
Makefile                     image build/push, plan, apply
```

[ADR-0002](../decisions/ADR-0002-terraform-manifest-boundary.md) lists "ECR repos, image build and
push" under Terraform. **Refinement:** `platform/` owns the *repository*; build and push are a
`make image` target, and `deployment/` consumes the result by digest. Terraform is a poor build tool,
and pinning by digest is what makes a deploy reproducible.

---

## 5. Site profile — one private file, nothing in the repository

Sites impose requirements on IAM role names and permissions boundaries, and the account id,
profile and bucket names are all account-identifying. **None of it may reach a tracked file**, in a
repository that is private today and may not stay that way. Scrubbing git history later is far more
expensive than getting this right once.

**There is exactly one private file: `.env`, gitignored.** `.env.example` is committed and shows the
shape. Nothing else in the repository names an account, a profile, a bucket, or a site requirement.

```bash
cp .env.example .env && $EDITOR .env
```

| `.env` holds | |
|---|---|
| `AWS_PROFILE` | The profile your login produces. `mosaic-gen` in these docs is a placeholder |
| `AWS_REGION` | Pinned to `us-west-2` by [08 §3](../specs/08-execution.md) |
| `STRATUM_DEPLOYMENT` | Deployment name; one deployment serves many runs |
| `STRATUM_PERMISSIONS_BOUNDARY_ARN` | Site requirement. **Present but empty** if the site imposes none |
| `STRATUM_ROLE_NAME_PREFIX` | Site requirement. Present but empty if none |

`scripts/common.sh` loads it and maps the friendly names onto the `TF_VAR_*` names Terraform reads,
so there is no `terraform.tfvars` to maintain and no second place for a value to hide.
`scripts/write-backend.sh` generates `terraform/backend.hcl` from the same file plus the account id
resolved at runtime — so even the state bucket name is never hand-written or committed.

The variables that consume them have **no defaults, on purpose**: a missing site value must fail the
plan loudly rather than silently apply something the site would reject.

```hcl
# terraform/platform/variables.tf
variable "permissions_boundary_arn" { type = string }
variable "role_name_prefix"         { type = string }

# terraform/platform/iam.tf - the whole mechanism, at the point of use
resource "aws_iam_role" "lambda" {
  name                 = "${var.role_name_prefix}stratum-${var.deployment}-lambda"
  permissions_boundary = local.boundary       # null when the site imposes none
  assume_role_policy   = data.aws_iam_policy_document.assume["lambda"].json
}
```

**SSM Parameter Store was considered and rejected** as over-engineering. Its only real advantage is
keeping values off a developer laptop; it does **not** protect state, because a data-source lookup
lands in state exactly as a variable does. Reach for it only if a site forbids the values on local
disk.

**The values reach Terraform state either way.** Unavoidable, and why §3 treats the state bucket as a
secret store. It is not an argument for one input mechanism over another.

**Extending it.** A second site is a second `.env`, selected with `STRATUM_ENV=…`. If EC2 is ever
needed, its site-specific parts — AMI or SSM parameter name, instance profile, VPC and subnet ids,
Session Manager configuration — are more variables of the same kind, not a new mechanism.

## 6. The deploy loop, and who runs what

Two personas, because the IAM split in §4 creates them.

| Step | Command | Persona | Frequency |
|---|---|---|---|
| 0 | `aws sso login --profile mosaic-gen` (or your SAML flow) | both | every session, ~hourly |
| 1 | `make bootstrap` | owner | once per account |
| 2 | `cp .env.example .env` and fill it in (§5) | both | once per workstation |
| 3 | `./scripts/put-edl-secret.sh` | owner | once, and on credential rotation |
| 4 | `make tf-init && make tf-apply` | **owner only** | rare — IAM, bucket, ECR |
| 5 | `make image` — build, tag by git SHA, push to ECR | deployer | every code or plugin change |
| 6 | `terraform -chdir=terraform/deployment apply -var image_digest=sha256:…` | deployer | to adopt a new image |
| 7 | `stratum run -m examples/emit-cmr-nevada-aws/manifest.yaml --executor aws` | anyone | every run |

**The boundary this encodes.** Step 7 alone is a science change. Steps 5 and 6 are code changes and
need no IAM rights, which is exactly the "amend a deployment" path: a new scorer, a new reader, a
changed threshold in code. Step 4 is the only privileged step and should be needed a handful of
times in the life of a deployment.

**A new selector is code, not configuration.** Scorers resolve through entry points declared in
`pyproject.toml` and read from installed metadata (`src/stratum/plugins.py`), so a *new* scorer class
must be in the image: steps 5 and 6. Selecting among *registered* scorers by name is step 7 only.
That is not a contradiction of [ADR-0002](../decisions/ADR-0002-terraform-manifest-boundary.md) —
the ADR puts plugin *packages* in the Code column and the scorer *choice* in the Manifest column.

**Concurrent deployers** share one state file per root, protected by `use_lockfile`. The root split
is what actually reduces contention: the frequently-applied root is small and touches nothing
privileged.

## 7. Credentials

Two independent chains. Confusing them is the most likely early mistake.

| | Your AWS identity | NASA Earthdata |
|---|---|---|
| Issued by | Your account | NASA URS / LP DAAC |
| Reaches | Your bucket, ECR, Step Functions, Batch | `s3://lp-prod-protected/…` |
| How a task gets it | Task/instance role, automatic | Fetched at runtime from Secrets Manager, then exchanged |
| Lifetime | Role session, SDK-refreshed | **~1 hour, nothing refreshes it for you** |

### The EDL exchange

Over HTTPS, the EDL username and password are the whole story — that is what
`access/auth.py:earthdata_login()` does today from `~/.netrc` or `EARTHDATA_USERNAME` /
`EARTHDATA_PASSWORD`. For **direct S3 access in-region**, the password is never used against S3.
The DAAC's credentials endpoint — LP DAAC: `https://data.lpdaac.earthdatacloud.nasa.gov/s3credentials`
— is called with EDL authentication and returns **temporary AWS credentials belonging to NASA**,
scoped to that bucket, valid about an hour. Those read the granules. `earthaccess` wraps the
exchange, and `auth.py` already builds its own `Auth` object rather than touching module state, so
it is positioned to call it.

### Rules

1. The EDL username and password live in **Secrets Manager**. Never in the image, never in git,
   never in a `.tfvars`, never in an environment variable baked into a job definition.
2. **Terraform creates the secret container and the IAM policy, never the value.**
   `lifecycle { ignore_changes = [secret_string] }`, populated by `scripts/put-edl-secret.sh`
   calling `aws secretsmanager put-secret-value`. Anything passed through Terraform lands in state,
   and state lives in the backend bucket (§3).
3. A **refresh wrapper** re-fetches the NASA credentials before expiry and wraps every long read.
   Lambda's 15-minute ceiling makes this a non-issue there; a three-hour Batch job dies at minute 61
   without it. This is the quiet argument in [08 §5](../specs/08-execution.md) for routing to Lambda
   wherever a work item fits.
4. Nothing logs, stores or raises a token or password. `EarthdataLoginError` names strategies and
   exception types only, and no credential reaches `plan.json`, the report or provenance. This
   property is already true and must survive the port.

---

## 8. Git hygiene

The `.gitignore` additions are applied in the repository ignore file. What matters and why:

| Pattern | Reason |
|---|---|
| `*.tfvars`, `*.tfvars.json` | Account ids, bucket names, and the one place a secret is most likely to be typed |
| `!*.tfvars.example` | The committed template is the documentation |
| `.terraform/` | Provider binaries and the resolved backend config |
| `terraform.tfstate*`, `*.tfstate.backup` | Local state should never exist, and if it does it must never be committed |
| `backend.hcl` | Real backend config; `backend.hcl.example` is committed |
| `crash.log`, `crash.*.log` | Terraform crash dumps embed variable values |
| `.env` | **The one private file.** Profile, region, deployment, site requirements. `.env.example` is committed |
| `.netrc`, `*.pem`, `*.p12`, `credentials` | Credential material of any kind |

**`.terraform.lock.hcl` is committed.** It is the provider dependency lock, the Terraform equivalent
of `pixi.lock`, and excluding it defeats reproducible plans.

**A tripwire, not just an ignore file.** ADR-0002 promises "a CI check that greps Terraform for zone
names, dates, and mineral identifiers" and no CI exists yet. The same check should refuse an
`AKIA`/`ASIA` access key id, a `secret_string` literal, and a `.tfvars` in the diff. Crude greps
catch the gradual leak, which is the leak that actually happens.

---

## 9. Build order — all-serverless

**Revised 2026-09-03: no EC2, and no Batch in the first cut.**

An earlier draft of this section proposed validating the S3 storage root on an EC2 instance under
the `local` executor. That was a detour. The storage root is testable from a laptop writing to a
bucket, and an instance running the local executor is not a cloud-native run in any sense that
matters — it is a local run with extra provisioning. EC2 is dropped from the plan entirely.

### Why this example fits Lambda whole

Every work item in `emit-cmr-nevada` clears Lambda's ceilings — 15 min, 10 GB memory, 10 GB `/tmp`
— by one to three orders of magnitude. Measured from the laptop run in
[status.html](../status.html) ("Archive run") and the per-stage constants in
[08 §7](../specs/08-execution.md):

| Stage | Items | Total | Worst single item | Headroom vs 900 s |
|---|---|---|---|---|
| plan | 1 | 93 s | 93 s, stages 1.55 GB to `/tmp` | 10x |
| regrid | 33 | 99 s | 19 s per granule + ~110 MB OBS download | 22x |
| resolve | 146 | 6 s | ~0.5 s per 720x720 block | 900x |
| reduce | 25 | 21 s | ~0.5 s per block | 300x |
| publish | 1 | 4 s | ~1.6 s per tile | 225x |

Memory is not close either: a whole-tile GLT is 0.14 GB and the compute unit is a 720x720 block.
The run stages 5.0 GB of assets in total, but no single invocation needs more than the granules for
its own item.

**Three consequences that shrink the work:**

1. **No Batch.** Nothing in this example needs it.
2. **No router.** [08 §2](../specs/08-execution.md)'s `route()` exists to choose between Lambda and
   Batch. With one destination there is no choice to make, so the missing per-stage constants and
   the unestimated `max_vcpu_hours` ([08 §7](../specs/08-execution.md) question 5) **stop blocking**.
   Slice 3 is unblocked by deletion rather than by measurement.
3. **No EC2 provisioning.** Lambda, Step Functions, S3, ECR and Secrets Manager only.

This is a property of this example, not a general result. A continental run will exceed 15 minutes
somewhere — most likely in plan, which is why [08 §2](../specs/08-execution.md) routes plan like any
other item and checkpoints per-tile work lists. Batch returns then, and §11 records the shape it
should take.

### The manifest delta

`examples/emit-cmr-nevada-aws/manifest.yaml` differs from `emit-cmr-nevada` in one field. That is
the point — the executor is not a manifest field ([ADR-0002](../decisions/ADR-0002-terraform-manifest-boundary.md)),
so the same science runs in both places:

```yaml
outputs:
  bucket: s3://stratum-dev-<account-id>/     # was: ./out
```

It is created in Slice 1, when the storage root can accept it. Creating it earlier would only add a
manifest that refuses at plan time.

### Orchestration: inline fan-out, not Step Functions

**Decided 2026-09-03.** The `aws` executor's first form is an **inline fan-out**: the orchestrator
walks the stages in order and invokes one Lambda per work item, concurrently, collecting outcomes.
Step Functions is deferred.

This is not a new design. `src/stratum/executors/local.py` already does exactly this shape — per
stage, submit every item to a pool, collect per-item outcomes into `work/{stage}.results.jsonl`,
fail all-or-nothing, then Finalize. The `aws` executor replaces the spawn-context
`ProcessPoolExecutor` with a thread pool calling `lambda_client.invoke()`. The stage order, the
outcome records, the budget gate and the exit codes are unchanged.

**The orchestrator is not infrastructure.** It is the CLI, so it runs wherever the CLI runs:

| Orchestrator location | What it needs | When |
|---|---|---|
| Your laptop | Credentials and network | Slice 2 — a real cloud fan-out with **no orchestration infrastructure at all** |
| An ECS task on Fargate | A task definition and a task role | Slice 3 — when a run should survive a closed laptop |

Identical code in both. That is what makes Slice 2 worth having on its own.

| | Inline fan-out | Step Functions |
|---|---|---|
| IAM roles | ~3 | ~5+, including Distributed Map |
| Orchestration logic | Python, debuggable locally | ASL + JSONata |
| Runs from a laptop | Yes | No |
| Retry and backoff | Ours, ~20 lines; content addressing makes retry safe ([08 §4](../specs/08-execution.md) rule 2) | Managed |
| Per-item outcomes | `work/{stage}.results.jsonl`, already built | `ResultWriter` |
| Concurrency ceiling | Thread pool; comfortable into the low thousands | 10,000 |
| Survives orchestrator death | No — but a resumed run is nearly all cache hits | Yes |

The first row decides it where IAM role creation is a rationed privilege. **A quiet bonus:** the
orchestrator never reads granules — the Lambdas do — and no Lambda runs past 15 minutes, so the
one-hour EDL expiry (§7) never arises on this path.

**Step Functions earns its place** at item counts approaching the thread pool's comfort, or a
delivered product needing a managed audit trail. Not at scheduling — that is EventBridge plus the
Slice 3 task definition (§9, Slice 4), and needs no state machine. It is additive: the work lists
are already S3 JSONL, which is what `ItemReader` consumes, so adopting it later replaces the
orchestrator and nothing else. [08 §1](../specs/08-execution.md)'s executor table should gain a line
recording that `aws` has two orchestration forms.

### Three separable questions

Orchestrator location, worker location and trigger are independent. Confusing them is the easiest
way to over-build a slice.

| Slice | Orchestrator | Work | Trigger | New infrastructure |
|---|---|---|---|---|
| 1 | laptop | laptop, process pool | a person | bucket |
| 2 | laptop | **Lambda** | a person | + one function, one image |
| 3 | **Fargate task** | Lambda | a person | + one task definition |
| 4 | Fargate task | Lambda | **EventBridge, monthly** | + a schedule |

**Slice 1 involves no container of any kind.** It is `stratum run` as it works today, with
`outputs.bucket` pointing at S3. The image does not appear until Slice 2, and ECS does not appear
until Slice 3.

### Slice 1 — the S3 storage root

Two streams, concurrent. A has no Python dependency; B needs only the bucket that A produces on its
first day. **Neither needs EC2, an image, or a Lambda.**

| Stream | Work |
|---|---|
| A | §3 bootstrap; §5 site parameters; `platform/` root — data bucket, ECR repo, EDL secret container, roles |
| B | `src/stratum/cache/` is `pathlib.Path`-native with temp-then-rename, and `plan/run.py:137 resolve_local()` refuses a remote `outputs.bucket`. Every stage writes through this layer. The long pole in the code |
| B | The run directory (`runs/{run_id}/`) moves too: `exec_item(plan_dir, …)` takes a path today, and a Lambda has nowhere local to read a plan from |

**Atomicity — resolved 2026-09-03.** The hazard: `hit()` skips a stage entirely, trusting the
artifact without re-reading it, and the key is a hash of the *inputs*, so a half-written artifact at
a key is undetectable and poisons every future run sharing that cache root
([08 §4](../specs/08-execution.md) rule 3).

| | Local, as built | On S3 |
|---|---|---|
| `write_file` (`cache/__init__.py:90`) | temp name, then `os.replace` | **Easier.** A single `PutObject` is atomic; a reader sees the old object or the new one, never a partial. Multipart likewise appears only on completion |
| `write_dir` (`:107`) | temp directory, sidecar inside, whole directory renamed | **No equivalent.** No directories and no rename — N objects under a prefix, written one at a time, with a window where some exist |

The existing design already carries the answer. `hit()` (`:80`) requires the artifact **and** its
`.inputs.json`, and the sidecar is committed last. On S3 that becomes: write the member objects,
then write the sidecar as its own final `PutObject`. A crash leaves members with no sidecar, which
is not a hit, so the stage recomputes and overwrites. **A mechanical port, not a redesign.**

Two narrower wrinkles remain, and one small change settles both:

1. **Stale members on rewrite.** Locally `write_dir` removes the old directory before renaming, so
   versions never mix. On S3, a new version with fewer members leaves the extras under the prefix.
2. **Concurrent writers on one key.** Locally the rename is clean last-writer-wins; on S3
   interleaved member writes could mix two writes. Content addressing says both workers compute the
   same artifact from the same inputs — true only if the computation is bit-deterministic.

**Decided: sidecar-last, and the sidecar lists its members.** Readers open only listed members, so
stale objects are ignored, and the sidecar remains the single commit point. Bit-determinism is worth
*verifying* in Slice 1 rather than assuming, but it is not a blocker: a mixed read cannot occur if
readers only open the members the winning sidecar names.

**Done when:** `stratum run -m examples/emit-cmr-nevada-aws/manifest.yaml` completes **from the
laptop** with `outputs.bucket: s3://…`, reading over HTTPS as it does today, and leaves the product,
STAC item and provenance in the bucket. Compare cell-for-cell against the recorded laptop run.

### Slice 2 — the image, the Lambda, and a cloud fan-out

| Work |
|---|
| Container image: the pixi environment plus `stratum`, two entrypoints — a Lambda Runtime Interface Client handler and the plain CLI. Container images, not zip layers ([08 §2](../specs/08-execution.md)) |
| The handler is thin: `{"plan": "s3://…", "stage": "regrid", "index": 7}` to `exec_item`, which already exists and is what every executor calls |
| EDL credential from Secrets Manager at runtime (§7), replacing `~/.netrc` |
| `deployment/` root: the function, its memory and timeout, pinned by image digest |
| The `aws` executor: thread pool over `lambda_client.invoke()`, retry with backoff, same outcome records as `local` |

**Done when:** `stratum run --executor aws` completes the whole Nevada run, **orchestrated from the
laptop**, with every work item executing in Lambda, and the product matches Slice 1 cell-for-cell.
That is `emit-cmr-nevada-aws` running cloud-native, with no ECS, no Step Functions and no EC2.

Invoking a single item remotely is worth keeping as a debugging affordance — it is how a
cloud-only failure gets diagnosed later.

### Slice 3 — detach the orchestrator

| Work |
|---|
| ECS task definition on Fargate running the same image with the CLI entrypoint |
| Task role: invoke Lambda, read and write the bucket, read the secret |
| `stratum run --executor aws --detach` submits the task and returns; `stratum status` reads the run directory from S3 |

**Done when:** a run survives a closed laptop and its result is identical to Slice 2's.

### Slice 4 — scheduled production runs

Slice 3 detaches the orchestrator; it does not remove the person. Production means a monthly run
that happens without one.

**Trigger.** EventBridge Scheduler on a cron, calling ECS `RunTask` against the Slice 3 task
definition with an overridden command. No state machine, no queue, no always-on compute.

**Where next month's manifest comes from.** Three options; the choice is load-bearing for
reproducibility.

| Option | Verdict |
|---|---|
| **Patch an absolute window at submit time** — `-p time.start=… -p time.end=…`, computed by the scheduled job | **Recommended** |
| A committed manifest per period | Reviewable and explicit, but a human in the loop and twelve near-identical files a year |
| A relative window in the manifest (`end: now`) | **Reject** |

Reject the relative window because the manifest hash is the reproducibility spine:
`run_id = f"{run_label}-{manifest_hash[7:15]}"` (`manifest/models.py:604`), and `TimeSpec.start`
and `end` are absolute, required `UtcDatetime`. A manifest that means something different depending
on when it is read makes its own hash meaningless and its provenance unreproducible.

The patch option keeps that intact: `manifest.merged.yaml` is "the document that was hashed"
(`plan/document.py:6`), so a patched run hashes the *resolved* document, earns its own `run_id`
automatically, and can be re-run verbatim from provenance years later.

**Consequence:** patch composition is the one piece of framework work this slice needs. `-p` is
accepted by the CLI and refused today (`cli.py:5`). It is specified, small, and it is what unblocks
scheduling.

**Persistent state is already there.** The S3 cache is the persistent storage. GLTs carry no
dependence on the scorer ([08 §6](../specs/08-execution.md)), so a monthly run regrids only granules
it has not seen and an overlapping window is nearly all cache hits. Nothing else needs to persist
between runs.

**Trigger on data, not only on the clock.** Cron alone fires whether or not the granules landed.
Start with a lag — run on the Nth for the previous period — and move to polling CMR for a stable
granule count only if the lag proves unreliable.

**Vintage, when this becomes a product.** Catalogue reprocessing against Tetracorder 6 starts
~September 2026 and runs ~75 days; mineral classes shift and the archive is mixed-vintage throughout
(`CLAUDE.md`, "Blocking context"). Vintage is checked by class-table fingerprint at plan time, and
`build_version` is filterable and reported but **not pinned**.

**This does not constrain the work in slices 1-4**, which validate mechanics against whatever data
is in the archive. It binds at the first run for record: pin `build_version` in the manifest, or let
the fingerprint check fail the run and alert. Decide then, not now.

**Done when:** a scheduled execution produces the previous month's product with no human involved,
and its provenance records a merged manifest that re-runs verbatim.

### Explicitly deferred

| Deferred | Until |
|---|---|
| Step Functions | Item counts beyond the thread pool, or a delivered product needing a managed audit trail. **Not** needed for scheduling. Additive — the work lists already suit `ItemReader` |
| AWS Batch | A work item actually exceeds a Lambda ceiling. On **Fargate**, so the deployment stays EC2-free |
| The `route()` function | Batch exists and there is a choice to make |
| Direct `s3://` reads of source granules | An optimisation, not a milestone. In-region only (`access/sources.py:371`), so it cannot be validated from a laptop; HTTPS works from Lambda and is the right default until measured otherwise |
| Per-stage cost constants, `max_vcpu_hours` | The router needs them; nothing else does |
| EC2 | A fixed-cost long campaign, if ever. Its site-specific parts are more §5 parameters, not a new mechanism |

## 10. Guardrails from day one

Cheap now, expensive to retrofit after a runaway fan-out.

| Guardrail | Why |
|---|---|
| Tag every resource and job with `run_id` and `deployment` | Cost allocation per run; ADR-0002 corollary 1 |
| An AWS Budget with an alarm before the first execution runs | A runaway fan-out is otherwise discovered by the bill. Cheap now, and the one guardrail that cannot be added retroactively to a run already in flight |
| `MaxConcurrency` per execution | One run cannot starve another in a shared deployment |
| S3 lifecycle rules on `cache/` | Content-addressed artifacts accumulate; nothing expires them |
| Log group retention set explicitly | The default is "never expire" and it is a real line item at 10k invocations |

---

## 11. Teardown

`terraform destroy` must leave the data bucket and the state bucket standing —
`prevent_destroy = true` on both. The cache is expensive to rebuild and the products are the
deliverable. Everything else in the deployment is reconstructible from this repository, which is
the property that makes an experimental deployment safe to tear down at all.

---

## 12. Open decisions

Ordered by when they must be answered.

| # | Decision | Needed by |
|---|---|---|
| 1 | ~~`write_dir` atomicity on S3~~ **Resolved** (§9, Slice 1): sidecar-last, sidecar lists its members. Residue: confirm regrid and resolve are bit-deterministic, so two workers on one key cannot disagree | Slice 1, as a test |
| 2 | **Image architecture.** `pyproject.toml` declares `platforms = ["osx-arm64", "linux-64"]` with no `linux-aarch64`, so a Graviton image cannot be built from this environment today. Add the platform (re-solves `pixi.lock`, needs a linux-aarch64 build of every conda dependency) or cross-build `linux/amd64` under emulation. Lambda and Fargate both support arm64 and it is cheaper | Slice 2 |
| 3 | **EDL secret shape** — password pair or a long-lived EDL bearer token. Affects `put-edl-secret.sh` and rotation | Slice 2 |
| 4 | **Image size and cold start.** A pixi environment with GDAL, netCDF4, scipy, rasterio and duckdb should sit well inside the 10 GB image limit, but cold start on a multi-GB image is seconds. A comfort question at 205 invocations, a cost question at 44,000 | Slice 2 |
| 5 | **Fan-out concurrency and Lambda reserved concurrency.** Unbounded invocation of 146 resolve items is fine; a continental run is not. Pick a default that cannot exhaust account concurrency and starve a concurrent run | Slice 2 |
| 6 | **Does the deployer persona need `terraform` at all**, or is `aws lambda update-function-code` enough for an image bump? Terraform keeps state honest; the CLI is one fewer tool for a scientist to install | Slice 2 |
| 7 | **Vintage policy for scheduled runs** — pin `build_version`, or let the class-table fingerprint check fail the run and alert. **Not a concern while validating mechanics on current data**; it binds before the first run for record | Before the first run for record |
| 8 | **Trigger style** — cron with a lag, or poll CMR for a stable granule count | Slice 4 |
| 9 | **One deployment, or separate accounts** for delivered products versus experimentation ([ADR-0002](../decisions/ADR-0002-terraform-manifest-boundary.md) corollary 4) | Before the first delivered run |
| 10 | **Batch on Fargate or EC2** — [08 §7](../specs/08-execution.md) question 1. Fargate keeps the deployment EC2-free but caps at 16 vCPU and 120 GB; EC2 gives better instance selection for memory-heavy reduces and local NVMe for staging. Prefer Fargate unless a measured item needs more | When Batch is needed |
