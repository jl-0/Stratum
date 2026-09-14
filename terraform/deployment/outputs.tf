output "function_name" {
  description = "Set $STRATUM_LAMBDA_FUNCTION to this, and `stratum run --executor aws` dispatches to it."
  value       = aws_lambda_function.worker.function_name
}

output "function_arn" { value = aws_lambda_function.worker.arn }

output "image_uri" {
  description = "What is deployed, by digest. This is the value provenance records as code.image_digest."
  value       = local.image_uri
}

output "log_group" { value = aws_cloudwatch_log_group.worker.name }

output "storage_root" {
  description = "Set outputs.bucket in a manifest to this."
  value       = local.platform.storage_root
}

output "run_command" {
  description = "Copy-paste: everything a cloud run needs beyond the manifest."
  value = join(" ", [
    "STRATUM_LAMBDA_FUNCTION=${aws_lambda_function.worker.function_name}",
    "stratum run -m <manifest.yaml> --executor aws",
  ])
}
