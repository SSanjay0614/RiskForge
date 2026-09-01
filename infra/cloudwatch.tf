# Phase 13 -- the dashboard, the alarms and the topic they notify.
#
# In Terraform rather than the console, for the same reason as Phases 10 and 11:
# a dashboard is a 200-line JSON document, and the thresholds below are only
# defensible if the reasoning sits next to the number. It also puts all of this
# in state, so `terraform destroy` takes the dashboard and the alarms with it
# instead of leaving them behind as the last manual items on the teardown list.
#
# Everything here is inside the free allowances: 3 dashboards, 10 alarms and
# 1,000 email notifications a month are free, and this file uses 1, 5 and
# approximately none. No Container Insights and no CloudWatch Agent, both of
# which bill -- see the note at the bottom of the dashboard for what that costs
# in visibility, because it is not nothing.

# ---------------------------------------------------------------------------
# Where an alarm goes
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name         = "${var.project}-alerts"
  display_name = "RiskForge alerts"
}

# No topic policy, deliberately. A CloudWatch alarm publishing to a topic in its
# own account is covered by the default policy AWS attaches, and a hand-written
# one here would replace that default -- which is an easy way to end up with a
# topic that alarms cannot publish to and that nobody notices is silent.
#
# No SSE-KMS either. An alarm message carries a metric name, a threshold and a
# timestamp; there is no borrower data in it to encrypt, and a customer-managed
# key would bill $1/month for a topic that sends a handful of mails.

resource "aws_sns_topic_subscription" "email" {
  # Absent an address the topic still exists and the alarms still work -- they
  # just show state in the console and mail nobody. Set alert_email in
  # terraform.tfvars to switch delivery on.
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email

  # Worth knowing before trusting this: an email subscription is created in
  # `PendingConfirmation` and delivers nothing until the link in the confirmation
  # mail is clicked. Terraform cannot see that state and reports the resource as
  # created either way, so a confirmed subscription and an ignored one look
  # identical in a plan. Check it once, by hand:
  #   aws sns list-subscriptions-by-topic --topic-arn <arn>
  # SubscriptionArn reads `PendingConfirmation` until the click, and an ARN after.
}

# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------

# treat_missing_data is set explicitly on every alarm below and the value is
# always notBreaching, which matters more here than it would on a service that
# runs continuously. This pipeline is invoked by one analyst and the database is
# stopped overnight, so "no data" is the normal state for most of the day. The
# CloudWatch default -- `missing`, meaning hold the previous state -- would leave
# an alarm latched in ALARM through every idle hour after a single failure.

resource "aws_cloudwatch_metric_alarm" "pipeline_failed" {
  alarm_name        = "${var.project}-pipeline-failed"
  alarm_description = "A pipeline execution ended in FAILED. This is the one alarm that maps directly to an analyst getting no answer."

  namespace   = "AWS/States"
  metric_name = "ExecutionsFailed"
  dimensions  = { StateMachineArn = aws_sfn_state_machine.pipeline.arn }

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "pipeline_timed_out" {
  alarm_name        = "${var.project}-pipeline-timed-out"
  alarm_description = "An execution hit pipeline_execution_timeout (1800s). Separate from the failure alarm on purpose: a timeout is a wedged Fargate task or a hung endpoint, not a refused question, and the two want different first moves."

  namespace   = "AWS/States"
  metric_name = "ExecutionsTimedOut"
  dimensions  = { StateMachineArn = aws_sfn_state_machine.pipeline.arn }

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "pipeline_slow" {
  alarm_name        = "${var.project}-pipeline-slow"
  alarm_description = "An execution succeeded but took more than ${var.pipeline_slow_threshold_ms / 1000}s. Neither of the alarms above can see this: a slow success is still a success, and degradation that never crosses into a timeout would otherwise be invisible until somebody complained."

  namespace   = "AWS/States"
  metric_name = "ExecutionTime"
  dimensions  = { StateMachineArn = aws_sfn_state_machine.pipeline.arn }

  # Maximum rather than Average. Executions arrive a few an hour at most, so an
  # average over a 5-minute period is usually one data point anyway -- and where
  # it is not, one slow run among three fast ones is exactly the thing worth
  # seeing rather than diluting.
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.pipeline_slow_threshold_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name        = "${var.project}-lambda-throttles"
  alarm_description = "Any Lambda in this account was throttled. Undimensioned on purpose -- the only functions here are RiskForge's five, so one alarm covers all of them instead of five near-identical ones."

  namespace   = "AWS/Lambda"
  metric_name = "Throttles"
  # No dimensions block: AWS/Lambda Throttles with no FunctionName aggregates
  # across every function in the region.

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "rds_memory" {
  alarm_name        = "${var.project}-rds-low-memory"
  alarm_description = "FreeableMemory below ${var.rds_low_memory_threshold_bytes / 1048576} MiB for 15 minutes. Threshold measured, not guessed -- see the variable."

  namespace   = "AWS/RDS"
  metric_name = "FreeableMemory"
  dimensions  = { DBInstanceIdentifier = aws_db_instance.main.identifier }

  statistic = "Minimum"
  period    = 300
  # Three periods, not one. A single dip below the line during a large scan is
  # the instance working, not the instance in trouble; fifteen sustained minutes
  # is the second one.
  evaluation_periods  = 3
  threshold           = var.rds_low_memory_threshold_bytes
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

locals {
  cw_region = var.aws_region

  # All five function names in one list so the Lambda widgets cannot drift out of
  # step with lambda.tf and lambda_agents.tf.
  lambda_names = concat(
    [aws_lambda_function.execute_sql.function_name,
    aws_lambda_function.compliance_check.function_name],
    [for k, f in aws_lambda_function.agent : f.function_name],
  )

  sagemaker_endpoints = [
    aws_sagemaker_endpoint.pd.name,
    aws_sagemaker_endpoint.lgd.name,
  ]
}

resource "aws_cloudwatch_dashboard" "overview" {
  dashboard_name = "${var.project}-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Pipeline outcomes"
          view   = "timeSeries", stacked = true, region = local.cw_region
          period = 300, stat = "Sum"
          yAxis  = { left = { min = 0, label = "executions", showUnits = false } }
          metrics = [
            for m in ["ExecutionsSucceeded", "ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted"] :
            ["AWS/States", m, "StateMachineArn", aws_sfn_state_machine.pipeline.arn, { label = m }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Pipeline execution time"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300
          yAxis  = { left = { min = 0, label = "ms", showUnits = false } }
          metrics = [
            ["AWS/States", "ExecutionTime", "StateMachineArn", aws_sfn_state_machine.pipeline.arn,
            { stat = "Maximum", label = "max" }],
            ["...", { stat = "Average", label = "avg" }],
          ]
          annotations = {
            horizontal = [
              { label = "slow alarm", value = var.pipeline_slow_threshold_ms, color = "#ff7f0e" },
              { label = "execution timeout", value = var.pipeline_execution_timeout * 1000, color = "#d62728" },
            ]
          }
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Lambda duration, max, against each timeout"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300, stat = "Maximum"
          yAxis  = { left = { min = 0, label = "ms", showUnits = false } }
          metrics = [
            for n in local.lambda_names :
            ["AWS/Lambda", "Duration", "FunctionName", n, { label = n }]
          ]
          # The three ceilings these can hit, drawn so the chart shows headroom
          # rather than a number that has to be looked up: compliance-check 30s,
          # execute-sql 60s, the three prompt functions 90s.
          annotations = {
            horizontal = [
              { label = "compliance-check timeout", value = 30000, color = "#c7c7c7" },
              { label = "execute-sql timeout", value = 60000, color = "#c7c7c7" },
              { label = "agent timeout", value = var.lambda_agent_timeout * 1000, color = "#c7c7c7" },
            ]
          }
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Lambda errors and throttles"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300, stat = "Sum"
          yAxis  = { left = { min = 0, label = "count", showUnits = false } }
          metrics = concat(
            [for n in local.lambda_names :
            ["AWS/Lambda", "Errors", "FunctionName", n, { label = "errors ${n}" }]],
            [["AWS/Lambda", "Throttles", { label = "throttles, all functions", color = "#d62728" }]],
          )
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 8, height = 6
        properties = {
          title  = "RDS freeable memory"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300, stat = "Minimum"
          yAxis  = { left = { min = 0, label = "bytes", showUnits = false } }
          metrics = [
            ["AWS/RDS", "FreeableMemory", "DBInstanceIdentifier", aws_db_instance.main.identifier]
          ]
          annotations = {
            horizontal = [
              { label = "alarm", value = var.rds_low_memory_threshold_bytes, color = "#d62728" },
            ]
          }
        }
      },
      {
        type = "metric", x = 8, y = 12, width = 8, height = 6
        properties = {
          title  = "RDS CPU"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300
          yAxis  = { left = { min = 0, max = 100, label = "percent", showUnits = false } }
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", aws_db_instance.main.identifier,
            { stat = "Maximum", label = "max" }],
            ["...", { stat = "Average", label = "avg" }],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 12, width = 8, height = 6
        properties = {
          title  = "RDS connections and free storage"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300, stat = "Maximum"
          metrics = [
            ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", aws_db_instance.main.identifier,
            { label = "connections", yAxis = "left" }],
            ["AWS/RDS", "FreeStorageSpace", "DBInstanceIdentifier", aws_db_instance.main.identifier,
            { label = "free storage bytes", yAxis = "right" }],
          ]
          yAxis = {
            left  = { min = 0, label = "connections", showUnits = false }
            right = { min = 0, label = "bytes", showUnits = false }
          }
        }
      },
      {
        type = "metric", x = 0, y = 18, width = 12, height = 6
        properties = {
          title  = "SageMaker latency: cold start against inference"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300, stat = "Average"
          yAxis  = { left = { min = 0, label = "microseconds", showUnits = false } }
          # setproduct, not a nested loop wrapped in flatten. Each metric here
          # is itself a list -- namespace, name, dimension pairs, then an
          # options object -- and flatten() recurses, so it dissolved those
          # inner lists into one flat run of strings. PutDashboard rejected the
          # body with "has to be an array of array of strings". setproduct
          # pairs the two dimensions and leaves one level of nesting intact.
          metrics = [
            for pair in setproduct(local.sagemaker_endpoints, ["ModelSetupTime", "OverheadLatency", "ModelLatency"]) :
            ["AWS/SageMaker", pair[1], "EndpointName", pair[0], "VariantName", "AllTraffic",
            { label = "${replace(pair[0], "${var.project}-", "")} ${pair[1]}" }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 18, width = 12, height = 6
        properties = {
          title  = "SageMaker invocations and errors"
          view   = "timeSeries", stacked = false, region = local.cw_region
          period = 300, stat = "Sum"
          yAxis  = { left = { min = 0, label = "count", showUnits = false } }
          # setproduct, not a nested loop wrapped in flatten. Each metric here
          # is itself a list -- namespace, name, dimension pairs, then an
          # options object -- and flatten() recurses, so it dissolved those
          # inner lists into one flat run of strings. PutDashboard rejected the
          # body with "has to be an array of array of strings". setproduct
          # pairs the two dimensions and leaves one level of nesting intact.
          metrics = [
            for pair in setproduct(local.sagemaker_endpoints, ["Invocations", "Invocation4XXErrors", "Invocation5XXErrors"]) :
            ["AWS/SageMaker", pair[1], "EndpointName", pair[0], "VariantName", "AllTraffic",
            { label = "${replace(pair[0], "${var.project}-", "")} ${pair[1]}" }]
          ]
        }
      },
      {
        type = "text", x = 0, y = 24, width = 24, height = 7
        properties = {
          markdown = <<-MD
            ### What is deliberately not on this dashboard

            **No Fargate CPU or memory.** The two risk agents run as standalone
            `run-task` invocations rather than as an ECS service, and a standalone
            task publishes no utilisation metrics without Container Insights,
            which bills per observation. The scoring branch is sized by
            `risk_task_cpu` and `risk_task_memory` and its real cost signal is
            wall-clock time, which the pipeline execution-time chart above already
            shows. Its logs are in `/ecs/${var.project}-risk`.

            **No process check on the Streamlit host.** Nothing here notices
            `riskforge.service` dying, because that needs the CloudWatch Agent on
            the instance. An EC2 status-check alarm was considered and left out on
            purpose: it detects a dead instance, not a dead unit, and the only
            user of this app is sitting in front of it and will notice the page
            failing to load before any alarm arrives. `journalctl -u riskforge` is
            the real tool. This is the gap to close first if the app ever gets an
            identity in front of it and more than one user.

            **No borrower data anywhere in here.** Every series above is a count,
            a duration or a byte figure. The state machine sets
            `include_execution_data = false`, so no payload reaches its log group
            either -- which is why there is no log-insights widget on this page.
          MD
        }
      },
    ]
  })
}
