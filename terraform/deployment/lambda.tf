# The worker: one work item per invocation (08 sections 1-2). `stratum run --executor aws`
# invokes it once per item of each stage and writes the outcomes; there is no state machine and
# nothing to provision for the orchestrator.

# Created explicitly rather than left to the function, which would create it with no retention.
resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "worker" {
  function_name = local.name
  role          = local.platform.lambda_role_arn
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
      STRATUM_EDL_SECRET = local.platform.edl_secret_arn

      # /tmp is the only writable filesystem here. The storage mirror and the staged granules
      # both live in it; neither is durable and neither needs to be.
      STRATUM_SCRATCH     = "/tmp"
      STRATUM_ASSET_CACHE = "/tmp/assets"
    }
  }

  # The log group must exist before the first invocation, or the function creates it without
  # retention and Terraform then fights it.
  depends_on = [aws_cloudwatch_log_group.worker]
}
