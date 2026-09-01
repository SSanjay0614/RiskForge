# The three prompt functions: Schema Guard, SQL generation, and the SQL
# evaluator.
#
# Phase 9 originally called for three Bedrock Agents, each fronting an Action
# Group Lambda. These are plain Lambdas instead, for two independent reasons.
#
# The first is architectural and would hold even on a paid account: an Agent
# earns its orchestration loop when the model decides which tool to call and
# iterates on what comes back. All three of these prompts are single-shot -- one
# prompt in, one strict JSON object out, no tool to call -- and the Step Functions
# state machine in Phase 11 already owns the sequencing. An Agent here would add
# an alias, a prepare lifecycle and billed orchestration turns in order to wrap a
# loop that never loops.
#
# The second is that Bedrock inference is not available on this account at all:
# every model, in two regions, on both Converse and InvokeModel, held by a
# principal with AdministratorAccess, returns "Operation not allowed", and
# `aws freetier get-account-plan-state` reports accountPlanType FREE. It is a plan
# restriction, like the one that blocked Aurora in Phase 2. See shared/gemini.py.
#
# None of the three is in the VPC. They call SSM and the model API, both public
# endpoints, so attaching them to a private subnet would mean an ENI plus either a
# NAT gateway or two interface endpoints, and a slower cold start, in exchange for
# nothing. The two functions that ARE in the VPC are there because the database
# has no public address.

locals {
  # Keys are short; `name` becomes riskforge-<name> and `zip` matches both the
  # directory under Deploy/lambda/ and the FUNCTIONS map in build.py.
  agent_functions = {
    guard = {
      zip         = "guard_action"
      name        = "guard-action"
      description = "Schema Guard: whether a question is answerable from the schema, and whether it needs the risk pipeline"
    }
    sqlgen = {
      zip         = "sqlgen_action"
      name        = "sqlgen-action"
      description = "Text-to-SQL: one read-only PostgreSQL SELECT, optionally corrected from evaluator feedback"
    }
    evaluator = {
      zip         = "evaluator_action"
      name        = "evaluator-action"
      description = "SQL evaluator: judges a result profile, never rows, and refuses an event that carries any"
    }
  }
}

# SecureString, so the value is encrypted with the account's aws/ssm KMS key and
# every read is a CloudTrail event.
#
# Parameter Store rather than Secrets Manager: rotation is Secrets Manager's
# differentiator, a third-party API key does not rotate on a schedule, and a
# standard parameter is free where a secret is $0.40/month. Identical guarantees,
# one of them free.
resource "aws_ssm_parameter" "gemini_api_key" {
  name        = var.gemini_api_key_param
  description = "API key for the model the three prompt functions call. Real value set out of band, never by Terraform."
  type        = "SecureString"
  value       = "PLACEHOLDER -- overwrite with: aws ssm put-parameter --name ${var.gemini_api_key_param} --type SecureString --value <key> --overwrite"

  lifecycle {
    # The real key is written out of band, and Terraform must not read it back,
    # diff it, or restore the placeholder over it. Without this the key would
    # have to arrive through a tfvars file or a -var flag to survive a plan --
    # which is exactly how a credential ends up in state, in a plan file, and in
    # shell history.
    ignore_changes = [value]
  }
}

resource "aws_lambda_function" "agent" {
  for_each = local.agent_functions

  function_name = "${var.project}-${each.value.name}"
  description   = each.value.description
  role          = aws_iam_role.agent[each.key].arn
  handler       = "handler.lambda_handler"
  runtime       = var.lambda_runtime
  architectures = ["arm64"]

  filename         = "${local.lambda_dist}/${each.value.zip}.zip"
  source_code_hash = filebase64sha256("${local.lambda_dist}/${each.value.zip}.zip")

  timeout     = var.lambda_agent_timeout
  memory_size = var.lambda_agent_memory

  environment {
    variables = {
      # The parameter's NAME, not its value. A Lambda's environment is readable
      # by anyone holding lambda:GetFunctionConfiguration and it is stored in
      # Terraform state -- so the key is fetched at runtime and cached in the
      # execution environment instead.
      GEMINI_API_KEY_PARAM   = aws_ssm_parameter.gemini_api_key.name
      GEMINI_MODEL           = var.gemini_model
      GEMINI_THINKING_BUDGET = tostring(var.gemini_thinking_budget)
    }
  }

  depends_on = [aws_iam_role_policy_attachment.agent_basic]
}

# Explicit, with retention, for the same reason as the other two: left to the
# service a log group is created on first invocation and never expires.
resource "aws_cloudwatch_log_group" "agent" {
  for_each = local.agent_functions

  name              = "/aws/lambda/${aws_lambda_function.agent[each.key].function_name}"
  retention_in_days = var.lambda_log_retention_days
}
