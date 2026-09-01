output "db_endpoint" {
  description = "host:port -- use this in connection strings (TLS is required)"
  value       = aws_db_instance.main.endpoint
}

output "db_name" {
  description = "Database created inside the instance"
  value       = aws_db_instance.main.db_name
}

output "db_secret_arn" {
  description = "Secrets Manager ARN holding the RDS-generated master credentials"
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "db_security_group_id" {
  value = aws_security_group.db.id
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "ec2_instance_id" {
  description = "Connect with: aws ssm start-session --target <this>"
  value       = aws_instance.app.id
}

output "ec2_public_ip" {
  description = "Streamlit will be at http://<this>:8501"
  value       = aws_instance.app.public_ip
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "lambda_security_group_id" {
  value = aws_security_group.lambda.id
}

output "pd_endpoint_name" {
  description = "SageMaker Serverless endpoint returning calibrated PD plus risk tier"
  value       = aws_sagemaker_endpoint.pd.name
}

output "lgd_endpoint_name" {
  description = "SageMaker Serverless endpoint returning LGD as a fraction of exposure"
  value       = aws_sagemaker_endpoint.lgd.name
}

output "execute_sql_function_name" {
  description = "Read-only SQL execution Lambda; results land in S3 under query-results/"
  value       = aws_lambda_function.execute_sql.function_name
}

output "compliance_check_function_name" {
  description = "Deterministic limit-checking Lambda; no LLM call anywhere in it"
  value       = aws_lambda_function.compliance_check.function_name
}

output "db_resource_id" {
  description = "Used in the rds-db:connect ARN for IAM database authentication"
  value       = aws_db_instance.main.resource_id
}

output "agent_function_names" {
  description = "The three prompt Lambdas, keyed guard/sqlgen/evaluator. Phase 11's state machine and Deploy/lambda/test_agents.py both address them by name."
  value       = { for key, fn in aws_lambda_function.agent : key => fn.function_name }
}

output "gemini_api_key_param" {
  description = "SSM parameter the three prompt functions read at runtime. Terraform creates it with a placeholder and ignores the value, so the real key is set with `aws ssm put-parameter --overwrite` and never enters state."
  value       = aws_ssm_parameter.gemini_api_key.name
}

output "risk_image_uri" {
  description = "Image the Fargate task definition runs. Push to it with Deploy/fargate/build_and_push.py."
  value       = local.risk_image
}

output "risk_cluster_name" {
  description = "ECS cluster the two risk tasks run in"
  value       = aws_ecs_cluster.risk.name
}

output "risk_task_definition" {
  description = "Task definition ARN, revision included. Step Functions and `aws ecs run-task` both address it by this."
  value       = aws_ecs_task_definition.risk.arn
}

output "pipeline_state_machine_arn" {
  description = "The Phase 11 state machine. Start an execution with Deploy/stepfunctions/run_pipeline.py, or `aws stepfunctions start-execution --state-machine-arn <this> --input '{\"query\": \"...\"}'`."
  value       = aws_sfn_state_machine.pipeline.arn
}

output "pipeline_state_machine_name" {
  value = aws_sfn_state_machine.pipeline.name
}

output "pipeline_log_group" {
  description = "State transitions, without payloads. The payloads are in the execution history (`aws stepfunctions get-execution-history`), which is where they belong."
  value       = aws_cloudwatch_log_group.pipeline.name
}

output "risk_task_network" {
  description = <<-EOT
    The awsvpc network configuration a run-task or Step Functions Parallel state
    has to supply. assign_public_ip is true and these are the default VPC's
    public subnets: the task needs to reach ECR, S3, SageMaker Runtime and
    CloudWatch Logs, and a public IP with an egress-only security group does that
    for nothing, where a NAT gateway is about $32/month and four interface
    endpoints about $30. Nothing can reach the task -- there is no ingress rule
    at all, and it holds no listening socket.
  EOT
  value = {
    subnets          = data.aws_subnets.db.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }
}
