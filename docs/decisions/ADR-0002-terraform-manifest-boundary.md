# ADR-0002 — The Terraform / manifest boundary

**Status:** proposed · **Date:** 2026-08-28

## Context

Stratum has two kinds of configuration: what the platform *is* (queues, roles, buckets, images)
and what a run *does* (grid, AOI, time range, cost function, colours). It is tempting to express
both in the tool already at hand — usually Terraform, because it is there first.

The failure mode is predictable and well-documented across data platforms: science parameters
leak into HCL, and then changing a cloud-fraction threshold requires an infrastructure PR, a plan
review, and an apply. The people the platform exists for stop using it directly, and a
platform-team bottleneck forms around parameter changes.

The specific risk here is that the science is *expected* to change constantly. Phil's instruction
was to start building and iterate; the whole design is optimized so that changing a cost function
is cheap. If that change also requires `terraform apply`, the optimization is wasted.

## Decision

**Terraform owns the platform. The manifest owns the science. Neither owns the code.**

| Terraform | Manifest | Code |
|---|---|---|
| ECR repos, image build/push | Grid, tiling, block size | Framework package |
| Batch compute envs, queues, job defs | AOI and zones | Plugin package |
| Lambda functions, Step Functions definition | Time range, epoch, cadence | Contracts, aux accessor |
| S3 buckets, lifecycle rules | Filters, masks, scorer, reducer, mapper | Tests and fixtures |
| IAM roles, EDL secret | Aux source declarations | |
| Budgets, alarms, dashboards | Budget ceilings, output formats | |

Corollaries:

1. **A new experiment is a new manifest, not a new deployment.** One Terraform deployment serves
   many concurrent runs, distinguished by `run_id` prefix and Step Functions execution.
2. **Terraform contains no zone names, no dates, no thresholds, no mineral classes.**
3. **Resource sizing is a platform concern**, but *which* resource a work item uses is a runtime
   routing decision from measured properties ([08 §2](../specs/08-execution.md)).
4. A second Terraform deployment is justified only by a real boundary — a separate AWS account
   for delivered products versus experimentation, or a different region — never by an experiment.

## Consequences

**Good.** Scientists change parameters without infrastructure access. Experiments are cheap and
concurrent. The manifest is reviewable in isolation and hashable into provenance. Infrastructure
changes are rare and therefore safe to gate carefully.

**Costs.** Two configuration systems to learn. Some genuinely ambiguous cases — is `block_size: 512` a
platform tuning knob or a run parameter? We put it in the manifest because it must be recorded in
provenance and may need to vary per workload, accepting that most users will never touch it.

**Enforced by.** A CI check that greps Terraform for zone names, dates, and mineral identifiers.
Crude, but the leak is gradual and a tripwire catches it early.

## Alternatives rejected

- **Everything in Terraform** — the failure mode above.
- **Everything in the manifest** (including infrastructure) — reinvents an IaC tool badly, and
  gives run authors the ability to change IAM.
- **A third config layer** for "operational" settings — plausible, but the split above has held
  for every case we have tested it against so far. Revisit if genuinely ambiguous settings
  accumulate.
