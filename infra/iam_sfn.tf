# The state machine's execution role. It calls five Lambda functions, runs one
# task definition on one cluster, and reads the two objects that task writes.
# Nothing in it can reach the database, a secret, the query-results prefix, or a
# SageMaker endpoint -- the states it orchestrates hold those permissions
# themselves, and the orchestrator does not inherit them.
#
# That split is the point of a state machine over a Lambda that calls other
# Lambdas: the thing deciding what happens next needs no access to the data any
# step handles.

resource "aws_iam_role" "sfn" {
  name = "${var.project}-pipeline-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "sfn_inline" {
  name = "${var.project}-pipeline-policy"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The five functions by ARN. The :* suffix covers published versions and
        # aliases of the same function, which is what a lambda:invoke task
        # resolves when a version is ever pinned.
        Sid    = "InvokeThePipelineFunctions"
        Effect = "Allow"
        Action = "lambda:InvokeFunction"
        Resource = flatten([
          for arn in concat(
            [aws_lambda_function.execute_sql.arn, aws_lambda_function.compliance_check.arn],
            [for fn in aws_lambda_function.agent : fn.arn]
          ) : [arn, "${arn}:*"]
        ])
      },
      {
        # Scoped to the task definition family, with a condition pinning the
        # cluster. Without the condition, a role that can run this task
        # definition can run it on any cluster in the account.
        Sid      = "RunTheRiskTaskOnThisClusterOnly"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.risk.family}:*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.risk.arn }
        }
      },
      {
        # runTask.sync polls DescribeTasks, and StopTask is what makes aborting an
        # execution actually stop the two containers instead of orphaning them to
        # run to completion and bill for it.
        Sid      = "WaitOnAndStopThoseTasks"
        Effect   = "Allow"
        Action   = ["ecs:DescribeTasks", "ecs:StopTask"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.risk.name}/*"
      },
      {
        # Handing the task its two roles. The condition is what stops this from
        # being a general-purpose privilege-escalation grant: these two ARNs, and
        # only to ECS.
        Sid    = "PassTheTaskRolesToEcsAndNothingElse"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.ecs_execution.arn,
          aws_iam_role.ecs_task.arn,
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        # The .sync pattern is implemented with a managed EventBridge rule that
        # notifies Step Functions when the task stops. Without these three the
        # runTask.sync state fails immediately with an EventBridge permissions
        # error, which reads like a problem with ECS. One rule, named by AWS.
        Sid      = "TheManagedRuleThatMakesSyncWork"
        Effect   = "Allow"
        Action   = ["events:PutRule", "events:PutTargets", "events:DescribeRule"]
        Resource = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"
      },
      {
        # Read the two aggregate objects the branches wrote, and nothing else.
        # Not the query-results prefix: the orchestrator has no business reading
        # borrower rows, and no state in the definition asks it to.
        Sid      = "ReadTheTwoBranchResults"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.artifacts.arn}/${var.risk_results_prefix}/*"
      },
      {
        # Log delivery, and the reason this one is a wildcard: the CreateLogDelivery
        # family of calls configures the delivery itself and takes no resource, the
        # same shape as ecr:GetAuthorizationToken in infra/iam_ecs.tf. AWS
        # documents this exact set for a state machine's logging configuration.
        # The log group it actually writes to is fixed in that configuration, not
        # chosen at runtime.
        Sid    = "VendLogsToCloudWatch"
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
    ]
  })
}
