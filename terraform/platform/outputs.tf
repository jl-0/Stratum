# Consumed by deployment/ through terraform_remote_state. deployment/ creates no IAM; it only
# references these ARNs.
output "data_bucket" { value = aws_s3_bucket.data.id }
output "data_bucket_arn" { value = aws_s3_bucket.data.arn }
output "ecr_repository_url" { value = aws_ecr_repository.stratum.repository_url }
output "edl_secret_arn" { value = aws_secretsmanager_secret.edl.arn }
output "lambda_role_arn" { value = aws_iam_role.lambda.arn }
output "task_role_arn" { value = aws_iam_role.task.arn }
output "task_exec_role_arn" { value = aws_iam_role.task_exec.arn }
output "storage_root" {
  description = "Set outputs.bucket in a manifest to this."
  value       = "s3://${aws_s3_bucket.data.id}/"
}
