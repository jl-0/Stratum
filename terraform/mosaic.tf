# `stratum mosaic` as a Fargate task: one web-mercator COG per layer of a published run
# (guide/viewing.html, stratum.publish.mosaic).
#
# Not a Lambda: a layer of US West merges ~400 tiles into one file of several gigabytes, which
# outlasts both the 15-minute limit and the 10 GB of /tmp. It is a batch job, started by
# `make cloud-mosaic RUN=<run id>` and gone when it finishes, so it bills only while it builds.
#
# It borrows the viewer's cluster, subnets and security group rather than growing its own: the
# task needs exactly what the viewer needs - ECR, S3 and CloudWatch Logs over 443, and DNS - and
# listens on nothing, so the viewer's one inbound rule admits nothing it serves. It runs the
# WORKER's image, because it is part of producing the products, not of looking at them.

locals {
  mosaic_name   = "stratum-${var.deployment}-mosaic"
  deploy_mosaic = local.deploy_worker && local.deploy_viewer
}

resource "aws_cloudwatch_log_group" "mosaic" {
  count             = local.deploy_mosaic ? 1 : 0
  name              = "/ecs/${local.mosaic_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "mosaic" {
  count = local.deploy_mosaic ? 1 : 0

  family                   = local.mosaic_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.mosaic_cpu
  memory                   = var.mosaic_memory_mb

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  # The task role already reads and writes the bucket's products/ (iam.tf).
  execution_role_arn = aws_iam_role.task_exec.arn
  task_role_arn      = aws_iam_role.task.arn

  # The sources stream from S3 by range and never land here; what does is each layer's output
  # while GDAL writes it, before it is uploaded and deleted - `mosaic_workers` of them at once.
  ephemeral_storage {
    size_in_gib = var.mosaic_storage_gib
  }

  container_definitions = jsonencode([{
    name  = "mosaic"
    image = local.image_uri

    # `--run` is appended per invocation (scripts/mosaic-task.sh), so one task definition serves
    # every run the deployment publishes.
    command = ["stratum", "mosaic",
      "--root", "s3://${aws_s3_bucket.data.id}/",
      "--workers", tostring(var.mosaic_workers),
    ]
    essential = true

    environment = [
      { name = "STRATUM_SCRATCH", value = "/tmp" },
      { name = "GDAL_CACHEMAX", value = "25%" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.mosaic[0].name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "mosaic"
      }
    }
  }])
}
