# ---------------------------------------------------------------------------------------------
# This file is the ONLY place IAM roles are created, and it is why the roots are split: at many
# sites only certain people may create roles, and role creation carries naming and boundary
# requirements. deployment/ creates no IAM, so anyone may apply it.
#
# How a site requirement reaches a resource: two variables with no defaults (variables.tf),
# filled in a gitignored terraform.tfvars, consumed below. That is the whole mechanism.
# ---------------------------------------------------------------------------------------------

locals {
  # "" means the site imposes no boundary; null omits the argument entirely.
  boundary = var.permissions_boundary_arn != "" ? var.permissions_boundary_arn : null
}

data "aws_iam_policy_document" "assume" {
  for_each = {
    lambda    = "lambda.amazonaws.com"
    task      = "ecs-tasks.amazonaws.com"
    task_exec = "ecs-tasks.amazonaws.com"
  }
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = [each.value]
    }
  }
}

# Read and write the deployment's own bucket: cache/, runs/, products/.
data "aws_iam_policy_document" "bucket_rw" {
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.data.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }
}

data "aws_iam_policy_document" "read_edl_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.edl.arn]
  }
}

# --- the Lambda worker: runs one work item (stratum exec) --------------------------------------
resource "aws_iam_role" "lambda" {
  name                 = local.role_name.lambda
  permissions_boundary = local.boundary
  assume_role_policy   = data.aws_iam_policy_document.assume["lambda"].json
}

resource "aws_iam_role_policy" "lambda_bucket" {
  role   = aws_iam_role.lambda.id
  name   = "bucket-rw"
  policy = data.aws_iam_policy_document.bucket_rw.json
}

resource "aws_iam_role_policy" "lambda_secret" {
  role   = aws_iam_role.lambda.id
  name   = "read-edl-secret"
  policy = data.aws_iam_policy_document.read_edl_secret.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- the orchestrator task (Slice 3): fans out to Lambda ---------------------------------------
# Created now although Slice 3 uses it: a privileged apply should be rare, so make the roles once.
resource "aws_iam_role" "task" {
  name                 = local.role_name.task
  permissions_boundary = local.boundary
  assume_role_policy   = data.aws_iam_policy_document.assume["task"].json
}

resource "aws_iam_role_policy" "task_bucket" {
  role   = aws_iam_role.task.id
  name   = "bucket-rw"
  policy = data.aws_iam_policy_document.bucket_rw.json
}

data "aws_iam_policy_document" "invoke_workers" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:stratum-${var.deployment}-*"]
  }
}

resource "aws_iam_role_policy" "task_invoke" {
  role   = aws_iam_role.task.id
  name   = "invoke-workers"
  policy = data.aws_iam_policy_document.invoke_workers.json
}

# --- the ECS task execution role: pulls the image, writes logs ---------------------------------
resource "aws_iam_role" "task_exec" {
  name                 = local.role_name.task_exec
  permissions_boundary = local.boundary
  assume_role_policy   = data.aws_iam_policy_document.assume["task_exec"].json
}

resource "aws_iam_role_policy_attachment" "task_exec" {
  role       = aws_iam_role.task_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
