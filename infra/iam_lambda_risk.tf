# One execution role per risk function, and they are not the same role.
#
# Both read the query result and write their own aggregate object. Only the score
# branch can invoke a SageMaker endpoint, because only the score branch has any
# code path that does -- the rates branch computes repricing gap and HHI out of
# raw columns and never scores anything. Giving it InvokeEndpoint would be a
# permission that could not be used, which is the kind of grant that survives
# unnoticed into an architecture where it can be.
#
# What neither role has is the more useful half of the list: no database, no
# rds-db:connect, no Secrets Manager, no Parameter Store, no ListBucket, and no
# write access to the prefix it reads from. By the time either function starts,
# riskforge-execute-sql has already put the rows in S3 -- the risk agents never
# touch PostgreSQL.
#
# There is no ECR statement and no AWSLambdaVPCAccessExecutionRole attachment,
# both deliberately. Lambda pulls a same-account container image under its own
# service principal rather than the function's role, so an ecr:BatchGetImage grant
# here would do nothing (contrast iam_ecs.tf, where the ECS *agent* pulls and
# needs it). And these functions are not in the VPC -- see the header of
# lambda_risk.tf for why -- so they create no ENI and need no permission to.

locals {
  risk_lambda_assume_role = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  # Read one prefix, write a different one. Shared by both roles because it is the
  # same S3 contract on both branches: the state machine hands over a key, and the
  # only object either function creates is the aggregate at the key it was given.
  risk_lambda_s3_statements = [
    {
      # No ListBucket. The function is given a key and has no business enumerating
      # what other questions have been asked.
      Sid      = "ReadTheQueryResult"
      Effect   = "Allow"
      Action   = "s3:GetObject"
      Resource = "${aws_s3_bucket.artifacts.arn}/${var.query_results_prefix}/*"
    },
    {
      # A different prefix from the one it reads, so a bug in the output path
      # cannot overwrite rows the parallel branch is still reading.
      # AbortMultipartUpload is what stops a failed write leaving billed,
      # invisible parts behind.
      Sid      = "WriteAggregateResults"
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
      Resource = "${aws_s3_bucket.artifacts.arn}/${var.risk_results_prefix}/*"
    },
  ]
}

resource "aws_iam_role" "risk_score" {
  name               = "${var.project}-risk-score-role"
  assume_role_policy = local.risk_lambda_assume_role
}

resource "aws_iam_role" "risk_rates" {
  name               = "${var.project}-risk-rates-role"
  assume_role_policy = local.risk_lambda_assume_role
}

resource "aws_iam_role_policy" "risk_score" {
  name = "${var.project}-risk-score-policy"
  role = aws_iam_role.risk_score.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.risk_lambda_s3_statements, [
      {
        # Kept even though SCORING_MODE defaults to local, because this is what
        # makes `--scoring endpoint` a working fallback rather than a claim. The
        # two endpoints by ARN, and Invoke only: this role cannot describe, update
        # or delete an endpoint, so it cannot change what the model is.
        Sid    = "ScoreAgainstThePdAndLgdEndpoints"
        Effect = "Allow"
        Action = "sagemaker:InvokeEndpoint"
        Resource = [
          aws_sagemaker_endpoint.pd.arn,
          aws_sagemaker_endpoint.lgd.arn,
        ]
      },
      {
        # The group itself is created in lambda_risk.tf, so CreateLogGroup is not
        # granted: if the name ever drifts, the function fails to log rather than
        # silently creating a second group with never-expiring retention.
        Sid      = "WriteItsOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.risk_score.arn}:*"
      },
    ])
  })
}

resource "aws_iam_role_policy" "risk_rates" {
  name = "${var.project}-risk-rates-policy"
  role = aws_iam_role.risk_rates.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.risk_lambda_s3_statements, [
      {
        Sid      = "WriteItsOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.risk_rates.arn}:*"
      },
    ])
  })
}
