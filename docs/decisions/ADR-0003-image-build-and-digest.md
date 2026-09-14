# ADR-0003 — The image is built by `make`, and a plugin is a distribution

**Status:** accepted · **Date:** 2026-09-14 · **Amends:** [ADR-0002](ADR-0002-terraform-manifest-boundary.md)

## Context

[ADR-0002](ADR-0002-terraform-manifest-boundary.md) put "ECR repos, image build/push" in the
Terraform column. The first half is right — the repository is infrastructure. The second half,
read literally, says Terraform builds the image, and the question that exposed it is a fair one:
*where is the Terraform that packages the science plugin?*

If the answer were "a `null_resource` that shells out to `docker build`", three things follow, all
bad. An apply would need Docker and the full source tree on the applier's machine, which defeats
the split that makes `deployment/` unprivileged. The build would run at apply time, so what is
deployed would depend on the working copy rather than on a recorded artifact. And adding a scorer
would become an infrastructure change — exactly the bottleneck ADR-0002 exists to prevent.

There is a related question underneath it: how does project-specific code reach a worker at all?
[`04 §7`](../specs/04-cost-functions.md) already answers it — entry points, delivered either in the
image or by a wheel URI in the manifest — but with `stratum_emit` shipping inside the framework's
own wheel, nothing exercised that path, and an unexercised seam is a claim rather than a mechanism.

## Decision

**Terraform never builds. It points at a digest.**

`make image` builds the container image and pushes it; `terraform/deployment/` takes the resulting
digest as an input variable and creates a Lambda from it. The ECR repository keeps
`image_tag_mutability = "IMMUTABLE"`, so a digest identifies bytes that cannot be replaced under it.

**A plugin is a Python distribution, never a Terraform resource.**

`stratum-emit` moves out of the framework's wheel into `plugins/stratum-emit/`, with its own
`pyproject.toml` carrying the `stratum.scorers`, `stratum.masks` and `stratum.readers` entry
points. The framework's distribution registers only `stratum.sources`. The image installs the two
distributions separately, which is precisely what a project's own plugin package will do.

Consequences for a project, in full:

| To… | Do | Privilege needed |
|---|---|---|
| Choose among registered plugins | Edit the manifest | none |
| Add a scorer, mask, reader or mapper | Add it to a plugin distribution, `make image`, `make deploy` | bucket + ECR write |
| Change a role, a bucket or a boundary | `terraform/platform/` | IAM |

A project writes no HCL at any point. It writes a `pyproject.toml` with entry points.

## Consequences

**Good.** What ran is identifiable from an immutable digest recorded in `provenance.json`
(`code.image_digest`, [10 §2](../specs/10-provenance.md)). `deployment/` stays appliable by someone
who cannot create IAM and does not have Docker. The plugin seam is exercised by the project's own
plugin package, so a third party's cannot be subtly harder to install than ours. `stratum plugins
list` inside an image answers "what does this deployment actually provide" without guessing.

**Costs.** Two distributions to version instead of one, and a deploy is two commands rather than
one apply. The manifest's `plugins.wheel` iteration path ([04 §7](../specs/04-cost-functions.md))
is still unbuilt, so today a new scorer means a new image — about a two-minute round trip, not the
ninety-second one that path promises.

## Alternatives rejected

- **Terraform builds and pushes the image** (`null_resource`, or the `docker` provider) — the three
  failure modes in the context above.
- **Keep one distribution and install it everywhere** — simpler, and it works, but it leaves the
  plugin seam untested by the only plugin we have.
- **Terraform reads a mutable tag** (`:latest`) — no apply is then reproducible, and the ECR
  repository's immutability setting would have to be given up to allow it.
