# What a run and a deploy need. The worker outputs are null until an image digest is set.

output "storage_root" {
  description = "Set outputs.bucket in a manifest to this."
  value       = "s3://${aws_s3_bucket.data.id}/"
}

output "data_bucket" { value = aws_s3_bucket.data.id }
output "ecr_repository_url" { value = aws_ecr_repository.stratum.repository_url }
output "edl_secret_arn" { value = aws_secretsmanager_secret.edl.arn }

output "lambda_role_arn" { value = aws_iam_role.lambda.arn }
output "task_role_arn" { value = aws_iam_role.task.arn }
output "task_exec_role_arn" { value = aws_iam_role.task_exec.arn }

output "function_name" {
  description = "Set $STRATUM_LAMBDA_FUNCTION to this, and `stratum run --executor aws` dispatches to it. Null until an image digest is set."
  value       = one(aws_lambda_function.worker[*].function_name)
}

output "function_arn" { value = one(aws_lambda_function.worker[*].arn) }
output "log_group" { value = one(aws_cloudwatch_log_group.worker[*].name) }

output "image_uri" {
  description = "What is deployed, by digest. This is what provenance records as code.image_digest."
  value       = local.deploy_worker ? local.image_uri : null
}

output "run_command" {
  description = "Copy-paste: everything a cloud run needs beyond the manifest."
  value = local.deploy_worker ? join(" ", [
    "STRATUM_LAMBDA_FUNCTION=${one(aws_lambda_function.worker[*].function_name)}",
    "stratum run -m <manifest.yaml> --executor aws",
  ]) : "set STRATUM_IMAGE_DIGEST in .env and re-apply: no worker is deployed"
}
