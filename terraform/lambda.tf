# The worker: one work item per invocation (08 sections 1-2). `stratum run --executor aws`
# invokes it once per item of each stage and writes the outcomes; there is no state machine and
# nothing to provision for the orchestrator.

# Created explicitly rather than left to the function, which would create it with no retention.
resource "aws_cloudwatch_log_group" "worker" {
  count = local.deploy_worker ? 1 : 0

  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

# NOT VPC-attached, deliberately, even when the deployment names a `vpc_id`. The worker downloads
# granules from the DAAC over the public internet (12 section 4); putting it in a VPC would mean a
# NAT gateway for that egress, plus an ENI per concurrent execution on the cold path. It reaches S3
# and Secrets Manager over AWS's own network either way. Attaching it is a real decision with a
# real bill, not a consequence of having a VPC.
resource "aws_lambda_function" "worker" {
  count = local.deploy_worker ? 1 : 0

  function_name = local.name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["arm64"]

  memory_size                    = var.memory_mb
  timeout                        = var.timeout_seconds
  reserved_concurrent_executions = var.reserved_concurrency

  ephemeral_storage {
    size = var.ephemeral_storage_mb
  }

  environment {
    variables = {
      # The secret's NAME, never its value: anything in this block is in Terraform state and in
      # the function configuration, where lambda:GetFunction can read it. The handler fetches
      # the value at run time (08 section 5).
      STRATUM_EDL_SECRET = aws_secretsmanager_secret.edl.arn

      # /tmp is the only writable filesystem here. The storage mirror and the staged granules
      # both live in it; neither is durable and neither needs to be.
      STRATUM_SCRATCH     = "/tmp"
      STRATUM_ASSET_CACHE = "/tmp/assets"

      # /tmp is 10 GB and BOTH of the things above grow: 109 MB of OBS per regrid item in
      # /tmp/assets, and in the mirror every artifact a stage pulls - dominated by the ortho
      # warps resolve reads, 89.2 GiB of them across the western run at ~12 MB each. Neither is
      # durable, so the guard is a free-space floor across
      # the whole volume rather than a budget per directory - capping one directory only decides
      # which one runs out first, which is how a 5500 MB asset cap starved the mirror and failed
      # 52,363 of 59,717 resolve items on ENOSPC.
      STRATUM_SCRATCH_RESERVE_BYTES = tostring(var.scratch_reserve_mb * 1024 * 1024)
      # Off: the per-directory policy cap is not what bounds this volume (see variables.tf).
      STRATUM_ASSET_CACHE_BUDGET_BYTES = tostring(var.asset_cache_budget_mb * 1024 * 1024)
    }
  }

  # The log group must exist before the first invocation, or the function creates it without
  # retention and Terraform then fights it.
  depends_on = [aws_cloudwatch_log_group.worker]
}
