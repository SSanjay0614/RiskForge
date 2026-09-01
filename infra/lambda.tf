# The two functions that need PostgreSQL, so the two that live in the VPC.
#
# Packages are built by Deploy/lambda/build.py and are not committed -- the zip
# is a build artifact, and a committed one drifts from the source beside it. Run
# the build before a plan; source_code_hash below is what makes Terraform notice
# a rebuild, and the build is deterministic so an unchanged source produces an
# identical zip and no spurious redeploy.

locals {
  lambda_dist = "${path.module}/../Deploy/lambda/dist"

  # Shared by both functions. No credential here and nothing to rotate: the
  # handler mints an IAM auth token per invocation.
  lambda_db_env = {
    DB_HOST = aws_db_instance.main.address
    DB_PORT = tostring(aws_db_instance.main.port)
    DB_NAME = var.db_name
    DB_USER = var.db_readonly_user
  }
}

resource "aws_lambda_function" "execute_sql" {
  function_name = "${var.project}-execute-sql"
  role          = aws_iam_role.lambda_execute_sql.arn
  handler       = "handler.lambda_handler"
  runtime       = var.lambda_runtime
  architectures = ["arm64"]

  filename         = "${local.lambda_dist}/execute_sql.zip"
  source_code_hash = filebase64sha256("${local.lambda_dist}/execute_sql.zip")

  # Long enough for a full scan of the 878k-row join plus the S3 upload, and
  # short of the 25s statement_timeout the database enforces plus headroom, so a
  # slow query dies as a clear PostgreSQL timeout rather than as an opaque
  # Lambda one.
  timeout = 60

  # 512 MB against a streaming writer that holds one 8 MB part at a time. Memory
  # also buys proportional CPU here, and CSV formatting of a large result set is
  # CPU-bound.
  memory_size = 512

  environment {
    variables = merge(local.lambda_db_env, {
      ARTIFACTS_BUCKET = aws_s3_bucket.artifacts.id
      RESULTS_PREFIX   = var.query_results_prefix
    })
  }

  vpc_config {
    subnet_ids         = data.aws_subnets.db.ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  depends_on = [aws_iam_role_policy_attachment.execute_sql_vpc]
}

resource "aws_lambda_function" "compliance_check" {
  function_name = "${var.project}-compliance-check"
  role          = aws_iam_role.lambda_compliance_check.arn
  handler       = "handler.lambda_handler"
  runtime       = var.lambda_runtime
  architectures = ["arm64"]

  filename         = "${local.lambda_dist}/compliance_check.zip"
  source_code_hash = filebase64sha256("${local.lambda_dist}/compliance_check.zip")

  # Five threshold rows and some comparisons. The only slow part is connection
  # setup.
  timeout     = 30
  memory_size = 256

  environment {
    variables = local.lambda_db_env
  }

  vpc_config {
    subnet_ids         = data.aws_subnets.db.ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  depends_on = [aws_iam_role_policy_attachment.compliance_check_vpc]
}

# Created explicitly rather than left to the service, which would create them on
# first invocation with never-expiring retention. Two weeks is longer than any
# debugging session here and stops the log group becoming the quiet line item on
# a $120 credit.
resource "aws_cloudwatch_log_group" "execute_sql" {
  name              = "/aws/lambda/${aws_lambda_function.execute_sql.function_name}"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_cloudwatch_log_group" "compliance_check" {
  name              = "/aws/lambda/${aws_lambda_function.compliance_check.function_name}"
  retention_in_days = var.lambda_log_retention_days
}
