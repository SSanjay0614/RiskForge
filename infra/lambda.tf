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

  # 900 seconds, the Lambda hard maximum, raised from 60. It is the outermost of
  # the four nested timeouts on this path -- see sql_statement_timeout_ms, which
  # documents the whole chain -- and it is set to the wall rather than to a
  # measured figure on purpose: everything inside it is bounded by a limit this
  # function controls, so the only thing this ceiling can now catch is the
  # platform failing in a way the others cannot see.
  timeout = 900

  # 3008 MB, raised from 512. Memory buys proportional vCPU and network bandwidth
  # on Lambda, and both matter here: the function reads a CSV stream from
  # PostgreSQL and writes it to S3 in 8 MB parts, so a whole-portfolio extract is
  # ~187 MB moving through it. It is not held -- COPY TO STDOUT means no row ever
  # becomes a Python object (see the handler's docstring), which is what makes
  # this size about throughput rather than about capacity.
  memory_size = 3008

  environment {
    variables = merge(local.lambda_db_env, {
      ARTIFACTS_BUCKET     = aws_s3_bucket.artifacts.id
      RESULTS_PREFIX       = var.query_results_prefix
      STATEMENT_TIMEOUT_MS = tostring(var.sql_statement_timeout_ms)
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
