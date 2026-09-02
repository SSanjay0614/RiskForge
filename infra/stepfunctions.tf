# The pipeline as one state machine: guard, generate, execute, evaluate, fan out
# to the two risk agents, then the compliance check.
#
# The definition is a separate file rather than a jsonencode() here, because a
# 380-line state machine inside HCL is a state machine nobody reads. Every ARN,
# subnet, bucket and prefix in it is a Terraform value substituted by
# templatefile(), so there is no copy of an ARN kept in step by hand -- and a
# task definition revision bump reaches the state machine on the next apply.
#
# STANDARD, not EXPRESS. Express workflows bill per request and keep no execution
# history: the whole audit trail would live in CloudWatch Logs at whatever
# retention was set. This pipeline runs a handful of times a day, answers
# questions about a credit portfolio, and its value is that you can open one
# execution months later and see the question, the generated SQL, the row count,
# both branch outputs and the limit check that followed from them. That is
# Standard's execution history, and at this volume the price difference is cents.

locals {
  pipeline_definition = templatefile("${path.module}/../Deploy/stepfunctions/pipeline.asl.json", {
    guard_arn       = aws_lambda_function.agent["guard"].arn
    sqlgen_arn      = aws_lambda_function.agent["sqlgen"].arn
    evaluator_arn   = aws_lambda_function.agent["evaluator"].arn
    execute_sql_arn = aws_lambda_function.execute_sql.arn
    compliance_arn  = aws_lambda_function.compliance_check.arn

    # The two risk agents. Container-image Lambdas running the identical image
    # the task definition below still points at -- see lambda_risk.tf for the
    # measurement that moved them off ecs:runTask.sync.
    risk_score_arn = aws_lambda_function.risk_score.arn
    risk_rates_arn = aws_lambda_function.risk_rates.arn

    # Kept, and currently unreferenced by the ASL. These are what an
    # ecs:runTask.sync branch needs, and leaving them wired means the revert is
    # an edit to pipeline.asl.json alone -- no Terraform change, no rebuild,
    # because entry.sh makes one image serve both runtimes. templatefile()
    # requires a value for every variable the template references and does not
    # object to one it does not.
    cluster_arn = aws_ecs_cluster.risk.arn
    # Revision included, deliberately: the state machine names the exact task
    # definition it was applied against, so a change to the container's CPU,
    # memory or image tag shows up as a change to this resource too rather than
    # silently altering what the pipeline runs.
    task_definition_arn  = aws_ecs_task_definition.risk.arn
    container_name       = local.risk_container
    subnets_json         = jsonencode(data.aws_subnets.db.ids)
    security_groups_json = jsonencode([aws_security_group.task.id])

    bucket              = aws_s3_bucket.artifacts.id
    risk_results_prefix = var.risk_results_prefix

    max_retries       = var.pipeline_max_retries
    task_timeout      = var.pipeline_task_timeout
    execution_timeout = var.pipeline_execution_timeout
  })
}

# /aws/vendedlogs/states/ is the path Step Functions' log delivery expects. A log
# group anywhere else works until AWS's resource policy for delivery hits its
# 5,120-character limit across accumulated groups, and then deliveries start
# failing for a reason that has nothing to do with this state machine.
resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/aws/vendedlogs/states/${var.project}-pipeline"
  retention_in_days = var.pipeline_log_retention_days
}

resource "aws_sfn_state_machine" "pipeline" {
  name       = "${var.project}-pipeline"
  role_arn   = aws_iam_role.sfn.arn
  type       = "STANDARD"
  definition = local.pipeline_definition

  logging_configuration {
    log_destination = "${aws_cloudwatch_log_group.pipeline.arn}:*"
    level           = "ALL"

    # Every state transition is logged; the payloads are not. The two are
    # separable and should be: the transitions are what you read to see where an
    # execution went, and the payloads carry the generated SQL, the result
    # profile and both metric sets. Those already live in the execution history,
    # which is scoped to states:DescribeExecution and expires in 90 days.
    # Copying them into a log group would put the same data behind a second,
    # broader permission with its own retention -- and if execute-sql ever
    # returns an inline row, that is where it would persist.
    include_execution_data = false
  }

  # No X-Ray. It bills per trace and would report the latency of seven Lambda
  # invocations -- which is what the execution history already shows, per state,
  # with the input and output attached.
}
