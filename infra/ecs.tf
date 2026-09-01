# The two risk agents as Fargate tasks: one cluster, one task definition, and a
# mode passed in per run. Step Functions (Phase 11) starts two of these at once
# in a Parallel state -- which is the whole reason they are separate tasks rather
# than one container computing both.
#
# There is no ECS *service* here, and that is the design rather than an omission:
# a service keeps N copies running to answer requests, and this is a batch job
# that starts, scores a query result, writes a JSON object and exits. Fargate
# bills per second of task runtime, so a run costs about a minute of 2 vCPU and
# nothing between runs.

locals {
  # :latest by default, so a rebuild reaches the next run without an apply. See
  # var.risk_image_tag for how to pin a specific build instead.
  risk_image = "${aws_ecr_repository.risk.repository_url}:${var.risk_image_tag}"

  # The container's name, which a caller has to know: a containerOverrides entry
  # is matched by name, and a mismatch is not an error -- the override is simply
  # ignored and the task runs with no command at all. Defined here and read by
  # infra/stepfunctions.tf so there is one spelling of it.
  risk_container = "risk"
}

resource "aws_ecs_cluster" "risk" {
  name = "${var.project}-cluster"

  # Container Insights is deliberately off. It bills as CloudWatch custom
  # metrics per task, and what it would report -- CPU and memory of a task that
  # runs for a minute -- is already in the task's own logs and the ECS console.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "risk_task" {
  name              = "/ecs/${var.project}-risk"
  retention_in_days = var.ecs_log_retention_days
}

# Egress only, and no ingress rule at all -- not "deny all", but no rule, so
# there is nothing for a later edit to widen. Nothing dials into this task: it is
# started by Step Functions, it fetches from S3, it calls two SageMaker
# endpoints, it writes to S3 and it exits.
resource "aws_security_group" "task" {
  name        = "${var.project}-task-sg"
  description = "RiskForge Fargate risk tasks: egress only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "task_all" {
  security_group_id = aws_security_group.task.id
  description       = "Outbound: ECR, S3, SageMaker Runtime, CloudWatch Logs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_ecs_task_definition" "risk" {
  family                   = "${var.project}-risk"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.risk_task_cpu)
  memory                   = tostring(var.risk_task_memory)

  # Two roles, because they are used at two different times by two different
  # things. The execution role belongs to the ECS agent and is used before the
  # container starts, to pull the image and open the log stream. The task role is
  # the one the Python process inside the container assumes, and it is the only
  # one that can reach S3 or a SageMaker endpoint.
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  # Stated rather than defaulted: the build machine is amd64 and so is this, but
  # an image built on an arm64 laptop fails at start with "exec format error",
  # which reads like a corrupt image rather than an architecture mismatch.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = local.risk_container
      image     = local.risk_image
      essential = true

      # No command. The mode, the input and the output are per-run values and
      # arrive as a containerOverrides command from Step Functions -- and the
      # Dockerfile deliberately has no CMD either, so a task started with no
      # override fails with a usage message instead of silently running whichever
      # branch happened to be the default.
      environment = [
        # Endpoint names rather than URLs, resolved from the resources
        # themselves: a rename in sagemaker.tf reaches the container on the next
        # task run, with no image rebuild.
        { name = "PD_ENDPOINT", value = aws_sagemaker_endpoint.pd.name },
        { name = "LGD_ENDPOINT", value = aws_sagemaker_endpoint.lgd.name },
        # boto3 finds the region from this. ECS provides credentials through the
        # task role's metadata endpoint but does not set a region, and a client
        # built without one fails with NoRegionError -- which looks like a
        # permissions problem and is not.
        { name = "AWS_DEFAULT_REGION", value = var.aws_region },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.risk_task.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "task"
        }
      }
    }
  ])
}
