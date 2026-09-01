# Two roles for the Fargate risk tasks, split by who uses them and when.
#
#   * ecs_execution is the ECS agent's, used before the container exists: pull the
#     image, create the log stream. The container's own code never holds it.
#   * ecs_task is the Python process's. It reads one query result, writes one
#     aggregate object, and invokes two SageMaker endpoints. Nothing else.
#
# Neither can read a secret, connect to the database, or write to the
# query-results prefix. The task never touches PostgreSQL -- riskforge-execute-sql
# already did, and the rows are in S3 by the time this starts.

locals {
  ecs_assume_role = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${var.project}-ecs-execution-role"
  assume_role_policy = local.ecs_assume_role
}

resource "aws_iam_role" "ecs_task" {
  name               = "${var.project}-ecs-task-role"
  assume_role_policy = local.ecs_assume_role
}

# Written out rather than attaching AmazonECSTaskExecutionRolePolicy, which
# grants image pulls from every repository in the account and log writes to every
# log group. There is one repository and one log group here, so the managed
# policy's convenience buys nothing and its scope is the whole account.
resource "aws_iam_role_policy" "ecs_execution_inline" {
  name = "${var.project}-ecs-execution-policy"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetAuthorizationToken takes no resource -- it returns a registry-wide
        # token and IAM rejects a policy that tries to scope it. The two calls
        # that actually read image content are scoped below.
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "PullTheRiskImageOnly"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.risk.arn
      },
      {
        Sid      = "WriteTaskLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.risk_task.arn}:*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "ecs_task_inline" {
  name = "${var.project}-ecs-task-policy"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read the one prefix that holds query results. No ListBucket: the task
        # is given a key by the state machine and has no business enumerating
        # what other questions have been asked.
        Sid      = "ReadTheQueryResult"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.artifacts.arn}/${var.query_results_prefix}/*"
      },
      {
        # Write aggregates only, and to a different prefix from the one it reads
        # -- so a bug in the output path cannot overwrite the rows a parallel
        # branch is still reading. AbortMultipartUpload is separate from
        # PutObject and is what stops a failed write leaving billed, invisible
        # parts behind.
        Sid      = "WriteAggregateResults"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.artifacts.arn}/${var.risk_results_prefix}/*"
      },
      {
        # The two endpoints by ARN, not sagemaker:* on a wildcard. Invoke is the
        # only SageMaker action the container makes: it cannot describe, update or
        # delete an endpoint, so a compromised task cannot change what the model
        # is.
        Sid    = "ScoreAgainstThePdAndLgdEndpoints"
        Effect = "Allow"
        Action = "sagemaker:InvokeEndpoint"
        Resource = [
          aws_sagemaker_endpoint.pd.arn,
          aws_sagemaker_endpoint.lgd.arn,
        ]
      },
    ]
  })
}
