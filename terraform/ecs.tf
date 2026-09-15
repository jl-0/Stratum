# The preview viewer as a Fargate task (guide/viewing.html).
#
# A debug tool: one container running `stratum preview` against the deployment's storage root,
# reachable on its private IP inside the VPC. It is started and stopped on demand - there is no
# service, no load balancer and no scaling - so it costs nothing when it is not running and there
# is nothing to keep patched between looks.
#
# Why a task and not a bucket policy: the products live beside `cache/` and `runs/` in a bucket
# whose public access block is on, and a browser cannot sign a request. The task can: it reads S3
# with the task role over the VPC's endpoint, and serves plain HTTP to the VPC. Nothing about the
# bucket changes.
#
# `var.vpc_id` is the only network input, the same way the permissions boundary is a site input
# (ADR-0002): this repository does not know your network. Everything else - the subnets, the
# security group - is derived from it or created here.

locals {
  viewer_name = "stratum-${var.deployment}-viewer"

  # One image, two pins. The viewer runs the same bytes as the worker by default, and a separate
  # digest decouples *when they move* rather than what is in them: a viewer rebuild then cannot
  # repoint a worker that is pinned for a campaign. `make image` still prints one digest; which
  # of the two lines in .env it lands on is the decision.
  viewer_digest = var.viewer_image_digest != "" ? var.viewer_image_digest : var.image_digest

  # Both halves are needed: an image to run, and a VPC to run it in. Absent either, this file
  # creates nothing and the rest of the root applies unchanged. Note it is the *viewer's* digest,
  # so a viewer may be deployed with no worker at all.
  deploy_viewer    = local.viewer_digest != "" && var.vpc_id != ""
  viewer_image_uri = local.deploy_viewer ? "${aws_ecr_repository.stratum.repository_url}@${local.viewer_digest}" : ""
}

# Every subnet in the VPC. Fargate picks one; if that one cannot reach ECR the task dies during the
# image pull, and `make viewer-up` prints the reason ECS gives rather than leaving you to find it.
data "aws_subnets" "viewer" {
  count = local.deploy_viewer ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [var.vpc_id]
  }
}

# --- the security group -------------------------------------------------------------------------
# Created here rather than asked for, because it is the only network control in front of a server
# with no authentication of its own, and a group inherited from elsewhere is a group whose rules
# nobody in this repository can see.
#
# The control is the PORT, not the source: one port in from anywhere that can route to the task,
# which - with no public IP, in a private subnet - means the VPC and whatever is peered to it.
# Egress is 443 and DNS, because Terraform drops the all-traffic rule AWS adds by default and
# nothing puts it back. A default group would let the task reach anything.

resource "aws_security_group" "viewer" {
  count = local.deploy_viewer ? 1 : 0

  name_prefix = "${local.viewer_name}-"
  vpc_id      = var.vpc_id
  description = "Stratum preview viewer: inbound ${var.viewer_port}, outbound HTTPS and DNS only"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "viewer" {
  count = local.deploy_viewer ? 1 : 0

  # NOTE: EC2 restricts a rule description to [a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*] - an apostrophe
  # is rejected at apply time, not at validate, so keep these plain.
  security_group_id = aws_security_group.viewer[0].id
  description       = "the viewer HTTP port"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = var.viewer_port
  to_port           = var.viewer_port
  ip_protocol       = "tcp"
}

# ECR, S3 and CloudWatch Logs - whether through a NAT gateway or a VPC endpoint, all of it is 443.
resource "aws_vpc_security_group_egress_rule" "viewer_https" {
  count = local.deploy_viewer ? 1 : 0

  security_group_id = aws_security_group.viewer[0].id
  description       = "ECR, S3 and CloudWatch Logs"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# Without these the task cannot resolve any of the above, and fails in a way that looks like a
# routing problem. The VPC resolver is inside the VPC, so the CIDR is the whole exposure.
resource "aws_vpc_security_group_egress_rule" "viewer_dns" {
  for_each = local.deploy_viewer ? toset(["tcp", "udp"]) : toset([])

  security_group_id = aws_security_group.viewer[0].id
  description       = "VPC DNS resolver (${each.value})"
  cidr_ipv4         = data.aws_vpc.this[0].cidr_block
  from_port         = 53
  to_port           = 53
  ip_protocol       = each.value
}

resource "aws_ecs_cluster" "viewer" {
  count = local.deploy_viewer ? 1 : 0
  name  = local.viewer_name
}

resource "aws_cloudwatch_log_group" "viewer" {
  count             = local.deploy_viewer ? 1 : 0
  name              = "/ecs/${local.viewer_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "viewer" {
  count = local.deploy_viewer ? 1 : 0

  family                   = local.viewer_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.viewer_cpu
  memory                   = var.viewer_memory_mb

  # The same architecture the image is built for (ADR-0003). Graviton, and cheaper.
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  # Pulls the image and writes logs; reads the bucket. Two roles, as ECS requires.
  execution_role_arn = aws_iam_role.task_exec.arn
  task_role_arn      = aws_iam_role.task.arn

  # The mirror: a preview session copies each raster it draws out of the bucket and renders from
  # disk. Sized for a browse, not for a run - it is thrown away with the task.
  ephemeral_storage {
    size_in_gib = var.viewer_storage_gib
  }

  container_definitions = jsonencode([{
    name  = "viewer"
    image = local.viewer_image_uri

    # `--no-open` because there is no browser here, and 0.0.0.0 because the client is the VPC and
    # not this container. The server has no authentication: the security group is the control.
    command = ["stratum", "preview",
      "--root", "s3://${aws_s3_bucket.data.id}/",
      "--host", "0.0.0.0", "--port", tostring(var.viewer_port), "--no-open",
    ]

    portMappings = [{ containerPort = var.viewer_port, protocol = "tcp" }]
    essential    = true

    environment = [
      # The mirror lives on the ephemeral volume, which is the only place with room for it.
      { name = "STRATUM_SCRATCH", value = "/tmp" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.viewer[0].name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "viewer"
      }
    }
  }])
}
