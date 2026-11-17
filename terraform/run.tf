# `stratum orchestrate` as a Fargate task: a whole run - plan, dispatch, Finalize - that does not
# depend on the machine that started it (08 section 1).
#
# The work still happens in the Lambda workers. What moves here is the loop that hands it out,
# which until now was `stratum run` on a laptop and stopped when the laptop did. Started by
# `make cloud-run-detached MANIFEST=...` and gone when the run finishes.
#
# It borrows the viewer's cluster, subnets and security group, as the mosaic task does, and for
# the same reason: it listens on nothing and needs only 443 and DNS out. Unlike the mosaic task
# it reaches beyond AWS - the planner searches CMR, stages one asset per role from the DAAC and
# fetches the aux sources - so those subnets must route to the internet.

locals {
  run_name = "stratum-${var.deployment}-run"
}

resource "aws_cloudwatch_log_group" "run" {
  count             = local.deploy_mosaic ? 1 : 0
  name              = "/ecs/${local.run_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "run" {
  count = local.deploy_mosaic ? 1 : 0

  family                   = local.run_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.run_cpu
  memory                   = var.run_memory_mb

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  # The task role is the orchestrator's: it reads and writes the bucket, invokes the workers,
  # and reads the Earthdata secret and the function's limits (iam.tf).
  execution_role_arn = aws_iam_role.task_exec.arn
  task_role_arn      = aws_iam_role.task.arn

  # What lands here: the staged bundle, the assets and aux sources the planner stages, and the
  # mirror of the run directory and of the published items Finalize reads.
  ephemeral_storage {
    size_in_gib = var.run_storage_gib
  }

  container_definitions = jsonencode([{
    name  = "run"
    image = local.image_uri

    # `--bundle` and `-m` are appended per invocation (scripts/run-task.sh), so one task
    # definition serves every manifest.
    command   = ["stratum", "orchestrate"]
    essential = true

    environment = [
      { name = "STRATUM_SCRATCH", value = "/tmp" },
      { name = "STRATUM_ASSET_CACHE", value = "/tmp/assets" },
      { name = "STRATUM_LAMBDA_FUNCTION", value = aws_lambda_function.worker[0].function_name },
      # The secret's NAME, never its value, exactly as for the worker (08 section 5).
      { name = "STRATUM_EDL_SECRET", value = aws_secretsmanager_secret.edl.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.run[0].name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "run"
      }
    }
  }])
}
