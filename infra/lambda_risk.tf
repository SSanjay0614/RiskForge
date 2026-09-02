# The two risk agents as container-image Lambda functions.
#
# They were Fargate tasks, and the measurement that moved them belongs next to the
# resource. Across five task lifecycles: 4-23s attaching an ENI, 8-17s pulling the
# 150 MB image, 3s starting -- 21-49 seconds before any application code ran. Then
# `ecs:runTask.sync` waits for STOPPED, and teardown was 10.0s of SIGTERM grace
# plus 14-19s of deregistration, so the state machine sat for a further 24-29
# seconds after the result was already in S3. The rates branch ran for 3.2 seconds
# inside a state that took 52.8.
#
# None of that is Fargate being slow. It is a per-request container lifecycle
# being the wrong shape for a request. Lambda caches the image layers rather than
# pulling them per invocation, keeps the execution environment warm between
# questions, and returns the moment the handler returns.
#
# **The same image, the same tag, the same digest** as the ECS task definition --
# see Deploy/fargate/entry.sh, which dispatches on AWS_LAMBDA_RUNTIME_API. That is
# what makes this reversible: infra/ecs.tf and the task definition are still here,
# so pointing the state machine back at ecs:runTask is an ASL edit and an apply,
# not a rebuild.
#
# **Not in the VPC, deliberately.** These functions reach S3 and, in the fallback
# scoring mode, SageMaker Runtime -- both public AWS API endpoints, reached over
# TLS with SigV4, neither inside the VPC. The Fargate task they replace ran with
# AssignPublicIp ENABLED on a public subnet, so this is not a reduction in
# posture; it is the same posture with one fewer ENI. What bounds these functions
# is IAM, in iam_lambda_risk.tf: one bucket prefix to read, a different one to
# write, two endpoint ARNs to invoke, and no database, no secret, no Parameter
# Store. Attaching them to the VPC would additionally break `--scoring endpoint`,
# because there is no SageMaker Runtime interface endpoint and no NAT gateway --
# costing the fallback that scoring_local.py is written to preserve.

locals {
  # Named here rather than read back off the function resources, so the log groups
  # do not depend on the functions. The functions depend on their roles, the roles'
  # policies depend on the log groups, and reading function_name back into a log
  # group name would close that into a cycle Terraform refuses to plan.
  risk_score_function = "${var.project}-risk-score"
  risk_rates_function = "${var.project}-risk-rates"

  # Every risk Lambda is this image with a different payload. Named once.
  risk_lambda_env = {
    # Recorded and reported rather than called, in the default scoring mode: the
    # output envelope names which models produced the numbers, and these are that
    # name. Passed from the resources so a rename in sagemaker.tf reaches the
    # function on the next apply with no image rebuild.
    PD_ENDPOINT  = aws_sagemaker_endpoint.pd.name
    LGD_ENDPOINT = aws_sagemaker_endpoint.lgd.name

    # local: the endpoints' own artifacts, through the endpoints' own handler, in
    # this process. endpoint: over SageMaker Runtime, ~880 requests for the whole
    # book. An environment variable rather than a payload field, so flipping the
    # whole pipeline back to the endpoints is one apply and touches no state
    # machine definition.
    SCORING_MODE = var.risk_scoring_mode

    # Read in endpoint mode only. Left set either way so a flip of SCORING_MODE
    # needs nothing else changed.
    WORKERS    = tostring(var.sagemaker_max_concurrency)
    BATCH_ROWS = tostring(var.sagemaker_batch_rows)

    # AWS_DEFAULT_REGION is deliberately absent. Lambda injects it -- and
    # AWS_REGION -- into every execution environment, and rejects CreateFunction
    # outright if the request also sets it: InvalidParameterValueException,
    # "reserved keys ... not supported for modification". ECS injects neither,
    # which is why ecs.tf sets it explicitly on this same image. boto3 finds the
    # region from the runtime here and from the task definition there.
  }
}

# score: feature engineering, PD, LGD, Expected Loss, Basel III RWA.
resource "aws_lambda_function" "risk_score" {
  function_name = local.risk_score_function
  role          = aws_iam_role.risk_score.arn
  package_type  = "Image"
  image_uri     = local.risk_image

  # x86_64, not the arm64 the zip functions use. This is the image ECS runs and it
  # is built amd64 -- see the runtime_platform block in ecs.tf. A mismatch here
  # fails at invoke with "exec format error", which reads like a corrupt image
  # rather than an architecture choice.
  architectures = ["x86_64"]

  # 10,240 MB, the Lambda maximum, and the reason is vCPU rather than capacity.
  # Memory and cores are the same dial on Lambda -- 10,240 MB is about six vCPU --
  # and pandas feature engineering over 878,317 rows is single-threaded and the
  # longest remaining step now that the round trips are gone. Capacity matters
  # too: the engineered frame plus get_dummies' intermediates peak near 3 GB on
  # the whole book. But 3 GB is what makes this size safe, not what makes it
  # necessary.
  #
  # Whether it is enough is measured rather than assumed. Every invocation's
  # REPORT line carries Max Memory Used, so the first whole-portfolio run states
  # the true peak -- which the Fargate task never did, because Container Insights
  # is disabled. If it ever approaches the ceiling the fix is scoring in row
  # slices, which is already safe and already implemented: scoring_local.py slices
  # at DEFAULT_PREDICT_ROWS, feature engineering has no cross-row operation in it,
  # and the credit aggregates are sums and counts.
  memory_size = var.risk_lambda_memory_mb

  # 900 seconds, the hard maximum. The work is 15-25 seconds warm, so this is not
  # a budget -- it is the wall that catches a platform failure the inner limits
  # cannot see. pipeline_task_timeout is the number that decides when to give up
  # on a question.
  timeout = var.risk_lambda_timeout

  # /tmp, and it is used rather than incidental: inputs.load_query_result stages
  # the S3 object through a NamedTemporaryFile before read_csv touches it, on
  # purpose -- a mid-transfer failure on a 300 MB object is then a retryable
  # download rather than a half-built DataFrame, and read_csv gets a seekable file,
  # which is what low_memory=False needs for single-pass type inference.
  #
  # The default is 512 MB and the whole book is ~177 MB of CSV, so the default
  # holds today. This is 2048 because the ceiling that actually applies is
  # inputs.MAX_ROWS at 1.2 million rows, which is a wider result than the whole
  # portfolio and would land near 250 MB -- and because /tmp on a warm environment
  # is shared with whatever the previous invocation left behind. inputs.py unlinks
  # in a finally block, so nothing should accumulate; this is the margin for the
  # case where something does. Ephemeral storage above 512 MB bills per GB-second
  # at a rate that makes this a rounding error against the function's own memory.
  ephemeral_storage {
    size = var.risk_lambda_tmp_mb
  }

  environment {
    variables = local.risk_lambda_env
  }

  # No reserved concurrency, and not by choice: Service Quotas L-B99A9384
  # (concurrent executions) is 10 on this account against AWS's published default
  # of 1000, and Lambda holds 10 back as the unreserved minimum -- so any
  # reservation at all is rejected with InvalidParameterValueException. Worth
  # knowing before a demo with more than one viewer: the fan-out uses two of those
  # ten per question, so several people asking at once would throttle.
}

# rates: repricing gap and Herfindahl-Hirschman concentration, on raw rows.
resource "aws_lambda_function" "risk_rates" {
  function_name = local.risk_rates_function
  role          = aws_iam_role.risk_rates.arn
  package_type  = "Image"
  image_uri     = local.risk_image
  architectures = ["x86_64"]

  # Smaller than the score branch because the work is smaller: three pandas
  # aggregations over the raw frame, measured at 3.2 seconds inside a 52.8-second
  # state. It still holds the whole raw frame -- inputs.py reads the CSV whole on
  # purpose, because RepricingGapTool takes its reporting date from max(issue_date)
  # across the entire population and ConcentrationTool needs every segment's total
  # -- so this is sized for the read, not for the arithmetic.
  memory_size = var.rates_lambda_memory_mb
  timeout     = var.risk_lambda_timeout

  # Same reasoning as the score branch: this one reads the identical CSV through
  # the identical staged download, so it needs the identical room to stage it.
  ephemeral_storage {
    size = var.risk_lambda_tmp_mb
  }

  environment {
    variables = local.risk_lambda_env
  }
}

# Created explicitly rather than left to the service, which would create them on
# first invocation with never-expiring retention.
resource "aws_cloudwatch_log_group" "risk_score" {
  name              = "/aws/lambda/${local.risk_score_function}"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_cloudwatch_log_group" "risk_rates" {
  name              = "/aws/lambda/${local.risk_rates_function}"
  retention_in_days = var.lambda_log_retention_days
}
