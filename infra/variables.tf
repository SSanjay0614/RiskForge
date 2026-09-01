variable "aws_profile" {
  description = "Local AWS CLI profile used for all API calls"
  type        = string
  default     = "riskforge"
}

variable "aws_region" {
  description = "Cheapest region with the widest Bedrock model coverage"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for all resources"
  type        = string
  default     = "riskforge"
}

variable "my_ip_cidr" {
  description = <<-EOT
    Your public IP as a /32, the only source allowed to reach the Streamlit
    port. Home broadband IPs rotate -- if the app stops loading, re-run
    `curl https://checkip.amazonaws.com`, update terraform.tfvars, re-apply.
  EOT
  type        = string
}

variable "db_name" {
  description = "Initial database created inside the PostgreSQL instance"
  type        = string
  default     = "riskforge"
}

variable "db_engine_version" {
  description = <<-EOT
    RDS PostgreSQL version. 16.15 is the latest 16.x in us-east-1, which keeps
    the major version the notebooks and the SQLite dump were written against.
    Check with:
    aws rds describe-db-engine-versions --engine postgres
  EOT
  type        = string
  default     = "16.15"
}

variable "db_instance_class" {
  description = <<-EOT
    db.t4g.micro: Graviton, 2 vCPU burstable, 1 GiB RAM, ~$0.016/hour in
    us-east-1, and one of the two classes the AWS free plan covers. Enough for
    a 1 GiB static snapshot read by one app.
  EOT
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = <<-EOT
    GiB. 20 is the gp3 floor on RDS, and the dataset needs under 1, so this is
    the minimum rather than a sizing decision. Autoscaling is off in rds.tf.
  EOT
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = <<-EOT
    Automated backup retention, in days. RDS itself allows 0-35, but the AWS
    free account plan caps this: CreateDBInstance rejects 7 with
    FreeTierRestrictionError, so this is 1 -- the lowest value that still keeps
    automated backups and point-in-time recovery switched on. 0 would disable
    them entirely. Little is lost here: the contents are a static snapshot
    rebuildable from the S3 dump, so backups are convenience, not the recovery
    plan. Raise this to 7 after any upgrade to a paid plan.
  EOT
  type        = number
  default     = 1
}

variable "ec2_instance_type" {
  description = "Build helper and Streamlit host"
  type        = string
  default     = "t3.small"
}

variable "sagemaker_xgboost_version" {
  description = <<-EOT
    Tag of the prebuilt SageMaker XGBoost image. The models were trained under
    xgboost 2.1.4, and SageMaker publishes no 2.x image at all -- the short tags
    available are 1.0-1, 1.2-1/2, 1.3-1, 1.5-1, 1.7-1, 3.0-5 and 3.2-0. So this
    goes forward to 3.0-5 rather than back to 1.7-1: XGBoost's JSON model format
    is read by later versions but not reliably by earlier ones. Because a version
    change could still move a prediction, the bundles pin iteration_range
    explicitly and Deploy/sagemaker/verify_endpoints.py replays fixed reference
    vectors against the live endpoints and fails on any difference at all.
  EOT
  type        = string
  default     = "3.0-5"
}

variable "sagemaker_memory_mb" {
  description = <<-EOT
    Serverless endpoint memory, which also determines the vCPU allocated. Valid
    values are 1024-6144 in 1024 steps. 2048 is comfortable for a 1000-tree
    XGBoost booster; raise it if the container logs an out-of-memory error, or to
    shorten cold starts, at a proportionally higher per-second price.
  EOT
  type        = number
  default     = 2048
}

variable "sagemaker_max_concurrency" {
  description = <<-EOT
    Concurrent invocations per endpoint before SageMaker throttles. One analyst
    and one Fargate scoring task do not need much; 5 leaves headroom for the
    Step Functions fan-out to score several segments at once without queueing.
  EOT
  type        = number
  default     = 5
}

variable "db_readonly_user" {
  description = <<-EOT
    The PostgreSQL role the Lambdas connect as. Created by
    Deploy/lambda/sql/create_readonly_role.sql: SELECT on the three tables,
    default_transaction_read_only = on, GRANT rds_iam so it has no password at
    all. This name appears inside the rds-db:connect ARN, so changing it here
    without re-running that file leaves the functions authorised to connect as a
    role that does not exist.
  EOT
  type        = string
  default     = "riskforge_ro"
}

variable "query_results_prefix" {
  description = <<-EOT
    S3 prefix execute-sql writes result sets to. Must stay in step with the
    expire-query-results lifecycle rule in s3.tf, which deletes this prefix after
    7 days -- borrower data extracted by a query should not outlive the question
    that asked for it.
  EOT
  type        = string
  default     = "query-results"
}

variable "lambda_runtime" {
  description = "Python runtime for both VPC-attached functions."
  type        = string
  default     = "python3.12"
}

variable "lambda_log_retention_days" {
  description = <<-EOT
    CloudWatch Logs retention for the Lambda log groups. Left to the service
    these default to never expiring, which on a fixed credit budget is a slow
    leak rather than a decision.
  EOT
  type        = number
  default     = 14
}

variable "gemini_model" {
  description = <<-EOT
    Model the three prompt functions call. Bedrock is unavailable on this
    account's AWS Free plan -- every model, both APIs, two regions, holding
    AdministratorAccess, returns "Operation not allowed", and
    `aws freetier get-account-plan-state` reports accountPlanType FREE. Rather
    than upgrade the plan and expose the running RDS and EC2 to real charges, the
    model host moved off AWS. Change this to any model the API key can reach and
    that serves generateContent with a responseSchema; a 2.5-family name
    additionally gets thinkingConfig, and nothing else does (see
    shared/gemini.py, where the gate is deliberately narrow).

    Gemma 4 31B is the deployed choice, and the reasons are capability and
    headroom rather than preference:

      * It is the family the prompts were developed against. The local build runs
        gemma4 on Ollama (config.py, llm/ollama_provider.py), so the hosted model
        is the same model class the guard, generation and evaluator prompts were
        written and tuned for -- not a substitute being assumed to behave alike.
      * Its free-tier request allowance is the largest of the candidates, which is
        what makes a 63-check verification suite runnable more than once a day.
      * It accepts responseSchema. That was the open question, since the API does
        not serve every family's models with structured output, and the answer
        decides whether the three handlers keep their contracts or grow a JSON
        scraper. Probed before switching: with responseMimeType application/json
        and a responseSchema it returns exactly the declared object. Without the
        schema the same prompt returns 300-400 tokens of visible reasoning and no
        JSON at all -- so the schema is not belt-and-braces here, it is the thing
        that makes the response parseable.

    The eliminations are the other useful half, because the model name is the one
    thing in this build that could not be reasoned about from documentation:

      * gemini-2.5-flash, which the local tools/ pointed at, returns 404 "no
        longer available to new users" for a key issued now. So do
        gemini-2.0-flash and gemini-2.5-flash-lite.
      * gemini-3.6-flash works, and its free-tier allowance is 20 requests per
        day for that model. Deploy/lambda/test_agents.py makes about twenty model
        calls, so one run of the suite exhausts a day. The provider reports it as
        RESOURCE_EXHAUSTED with "retry in 59s", which reads like a per-minute
        limit and is not one.
      * gemma-3-27b-it is not served by generateContent on v1beta at all. Worth
        keeping because it is the near miss: the family is available, that
        specific model is not, and probing one member of a family does not settle
        the others. gemma-4-31b-it and gemma-4-26b-a4b-it both are served.
      * gemini-3.5-flash-lite works and passed the whole suite, so it stands as
        the tested fallback if the Gemma quota ever tightens -- one variable, no
        rebuild.

    Any name here is checked against the API rather than trusted: GET /v1beta/models
    lists what a given key can actually reach and which methods each model
    supports, which is a five-second call and settles in advance what would
    otherwise be a 404 discovered in a deployed function.

    The model name is an environment variable rather than a constant in the
    handlers precisely so all of that was configuration and not five rebuilds.
  EOT
  type        = string
  default     = "gemma-4-31b-it"
}

variable "gemini_api_key_param" {
  description = <<-EOT
    SSM Parameter Store path holding the API key as a SecureString. Terraform
    creates the parameter with a placeholder and then ignores its value, so the
    real key is written once with `aws ssm put-parameter --overwrite` and never
    enters Terraform state, a plan file, or this repository.
  EOT
  type        = string
  default     = "/riskforge/gemini-api-key"
}

variable "gemini_thinking_budget" {
  description = <<-EOT
    Output tokens the model may spend thinking before it answers. Zero for these
    three: two are classifiers and one emits a single SELECT, and a thinking
    budget that runs out returns finishReason MAX_TOKENS with empty text, which
    reads like a refusal rather than a truncation.
  EOT
  type        = number
  default     = 0
}

variable "lambda_agent_timeout" {
  description = <<-EOT
    Timeout for the three prompt functions. Generous on purpose: gemini.py
    retries a rate-limited call up to three times with jittered backoff, and a
    free-tier key's requests-per-minute ceiling is the failure that actually
    happens when three steps run back to back. Lambda bills for milliseconds
    used, not for the ceiling.
  EOT
  type        = number
  default     = 90
}

variable "lambda_agent_memory" {
  description = "Memory for the three prompt functions. They hold one prompt and one JSON response; memory here buys CPU for TLS and JSON, not headroom."
  type        = number
  default     = 256
}

variable "risk_image_tag" {
  description = <<-EOT
    Tag of the image the task definition runs. `latest` by default, which is what
    Deploy/fargate/build_and_push.py re-points on every push -- so a rebuild
    reaches the next task run without a Terraform apply.

    Every push also gets an immutable tag: the first 12 characters of the SHA-256
    of the build manifest, which is a digest of every file that went into the
    image. Set this to one of those to pin a task definition to a specific build,
    which is what you want if a number ever needs reproducing months later.
  EOT
  type        = string
  default     = "latest"
}

variable "risk_task_cpu" {
  description = <<-EOT
    Fargate task vCPU, in units of 1024. 2048 (2 vCPU) because the container
    holds the whole query result as a DataFrame and runs five concurrent HTTPS
    calls to the SageMaker endpoints while pandas engineers the next batch.

    Fargate bills per second for exactly this, so the size is a wall-clock
    decision, not a monthly one: doubling the CPU on a 40-second task costs
    another 40 seconds of the larger size.
  EOT
  type        = number
  default     = 2048
}

variable "risk_task_memory" {
  description = <<-EOT
    Fargate task memory in MiB. Valid pairings with 2048 CPU are 4096-16384 in
    1024 steps. 8192 because the working set is the raw frame plus the engineered
    frame plus the batch matrices, and the raw frame for the whole 878k-row book
    is around 1.5 GB before any of that -- Deploy/fargate/riskforge/inputs.py caps
    the row count at 1.2 million for the same reason.
  EOT
  type        = number
  default     = 8192
}

variable "risk_results_prefix" {
  description = <<-EOT
    S3 prefix the Fargate tasks write their aggregates to. Deliberately not
    query-results: that prefix is expired after 7 days because it holds extracted
    borrower rows, while these objects are sums and weighted averages with no row
    in them (enforced by Deploy/fargate/riskforge/outputs.py) and are the audit
    trail for what a run reported.
  EOT
  type        = string
  default     = "risk-results"
}

variable "ecs_log_retention_days" {
  description = "CloudWatch Logs retention for the Fargate task's log group. Same reasoning as lambda_log_retention_days: the service default is never."
  type        = number
  default     = 14
}

variable "pipeline_max_retries" {
  description = <<-EOT
    How many times the state machine may regenerate SQL before giving up. Three,
    carried across from the local workflow's max_retries, and the number matters
    in both directions: fewer than three and a first attempt that missed a filter
    has no room to be corrected twice, more than three and a question the schema
    genuinely cannot answer costs four model calls and four database round trips
    to arrive at the same refusal. The counter lives in the execution's own JSON,
    incremented by a Pass state, so it is visible in the history rather than
    hidden in a service's retry policy.
  EOT
  type        = number
  default     = 3
}

variable "pipeline_task_timeout" {
  description = <<-EOT
    Seconds a single Fargate branch may take before Step Functions abandons it.
    900 against a scoring branch that takes about 26 seconds: the margin is not
    for slow scoring but for a task stuck in PROVISIONING, which is the failure
    this timeout exists to end. Long enough that a genuinely large population is
    never cut off mid-run, short enough that a wedged task does not hold an
    execution open for the full 1800.
  EOT
  type        = number
  default     = 900
}

variable "pipeline_execution_timeout" {
  description = <<-EOT
    Seconds an execution may run in total. A Standard workflow's default is a
    year, which is the wrong default for a question somebody is waiting on: an
    execution that hangs should end and say so. 1800 is roughly twenty times the
    observed end-to-end time.
  EOT
  type        = number
  default     = 1800
}

variable "pipeline_log_retention_days" {
  description = "CloudWatch Logs retention for the state machine's transition log. Shorter than the 90-day execution history on purpose: the history is the audit trail, this group is for watching a run go past."
  type        = number
  default     = 14
}
