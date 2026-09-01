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
    values are 1024-6144 in 1024 steps.

    3072, and this is the account's ceiling rather than a choice. The reason to
    raise it at all is cold start, not capacity: a 1000-tree XGBoost booster is
    comfortable in 2048, which is what this was, but ModelLatency runs 78-177 ms
    against a ModelSetupTime of 8.3-8.9 s and an OverheadLatency of 5.4-6.6 s.
    The model is two orders of magnitude faster than the act of waking it up, and
    memory is the only lever this endpoint type gives on that.

    6144 was applied for and refused: `ResourceLimitExceeded: The account-level
    service limit 'Memory size in MB per serverless endpoint' is 3072 MBs`. That
    limit is not listed in Service Quotas at all -- the error's own fallback
    advice is to open a support case -- so 3072 is the highest value this account
    can apply until one is granted. Serverless bills per GB-second of invocation
    duration, so the higher tier is not free; it is a trade of per-second price
    against the setup time that dominates a whole-portfolio run.
  EOT
  type        = number
  default     = 3072
}

variable "sagemaker_max_concurrency" {
  description = <<-EOT
    Concurrent invocations per endpoint before SageMaker throttles. Multiplied by
    the number of endpoints, this is spent against one account-wide budget.

    5, and unlike every other number in this file it is not sized to the
    workload -- it is the whole quota, divided by two. Service Quotas
    L-96300102, "Maximum total concurrency that can be allocated across all
    serverless endpoints", is 10 on this account, and PD and LGD are two
    endpoints, so 5 each is the arithmetic. An apply at 50 fails at the
    UpdateEndpoint call, not at plan time.

    What that costs is worth stating plainly, because it is the binding
    constraint on the headline question: the whole 878,317-row book splits into
    batches, and at 5 concurrent the task makes many more sequential round trips
    than the endpoint's own speed would require. AWS's published default for this
    quota is 200 -- 10 is a new-account throttle -- so an increase request is the
    real fix. Once it is granted, raise this to 50 and re-apply; nothing else has
    to change, because sagemaker_batch_rows and the task's --workers both follow
    from it.
  EOT
  type        = number
  default     = 5
}

variable "sagemaker_batch_rows" {
  description = <<-EOT
    Rows per inference request, passed to the task as BATCH_ROWS.

    3,000, raised from the 2,000 compiled into scoring.py, and this is the one
    lever on round-trip count that does not need a quota increase. With
    concurrency pinned at 5 by L-96300102, the only way to cut sequential rounds
    is to put more rows in each request: 878,317 rows is 440 batches at 2,000 and
    293 at 3,000, so a third of the round trips disappear for free.

    Not higher, because a Serverless endpoint's request body limit is 4 MB and
    scoring.py budgets 3 MB of it. At roughly 800 bytes of JSON per row that puts
    the ceiling near 3,750, and a batch that does come out oversized is split in
    half and re-sent rather than rejected -- so overshooting costs a wasted round
    trip, which is exactly what this is trying to save.
  EOT
  type        = number
  default     = 3000
}

variable "sql_statement_timeout_ms" {
  description = <<-EOT
    The statement budget the execute-sql function sets on its own session, in
    milliseconds, and the ceiling a question has to answer within.

    600,000, raised from a literal 25,000 in Deploy/lambda/shared/db.py. 25
    seconds was measured against a filtered question and was right for one; it
    was wrong for the question this system is built to answer. The whole-portfolio
    join scans 878,317 rows in 6.7 seconds server-side and the extract that
    follows takes minutes, so a 25-second ceiling did not refuse a careless query,
    it refused the headline one -- and refused it as `57014 statement timeout`,
    which the state machine maps to QueryTooBroad. The largest legitimate question
    in the portfolio reported itself as too vague to answer.

    Four timeouts have to nest in this order, each strictly containing the one
    inside it, or a legal query dies as the wrong kind of error:

      statement_timeout       600s  this variable, enforced by PostgreSQL
      pg8000 socket read      605s  derived in db.py as this + 5
      execute-sql Lambda      900s  the Lambda hard maximum
      Step Functions task    1800s  pipeline_task_timeout

    900 is a wall, not a choice -- no Lambda may run longer -- so 600 here is what
    leaves the server room to end its own query and say so before the platform
    kills the function underneath it.
  EOT
  type        = number
  default     = 600000
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
    Fargate task vCPU, in units of 1024. 4096 (4 vCPU), raised from 2048.

    Two of those four are for the scoring thread pool, which is network-bound
    against the endpoints and releases the GIL, so it scales with cores once
    --workers is raised past 5. The other two are the honest limit of this: pandas
    feature engineering across 878k rows is single-threaded, and no core count
    fixes that. 8 vCPU was considered and rejected for exactly that reason -- it
    would double the per-second price to leave four cores idle.

    Fargate bills per second for exactly this, so the size is a wall-clock
    decision, not a monthly one: doubling the CPU on a 40-second task costs
    another 40 seconds of the larger size.
  EOT
  type        = number
  default     = 4096
}

variable "risk_task_memory" {
  description = <<-EOT
    Fargate task memory in MiB. Valid pairings with 4096 CPU are 8192-30720 in
    1024 steps.

    30720, the ceiling, raised from 8192 -- and this one is bought as insurance
    rather than for throughput. The working set on a whole-portfolio run is the
    raw 878k-row frame (~1.5 GB), plus the engineered frame after one-hot
    expansion, plus the batch matrices in flight, and pandas transiently holds
    two copies of a frame during several of those operations. 8192 was sized
    against a filtered question and would very likely have met the headline one
    with an OOM kill -- which Fargate reports as a bare non-zero exit that the
    state machine does not retry, so it would have surfaced in a demo as the
    pipeline simply failing.

    Memory alone is close to free next to the vCPU it is paired with, so there is
    no argument for cutting this fine.
  EOT
  type        = number
  default     = 30720
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

    1800, raised from 900. The old value carried a margin of thirty-odd times the
    observed scoring time, which sounded generous and was measured against a
    16,000-row question. A whole-portfolio branch is 878,317 rows: minutes of
    feature engineering and 439 scoring batches, on top of the cold start the
    margin was actually there for. 900 would have cut the headline run off
    mid-scoring and reported it as a task timeout.

    Still bounded, and that is the point -- this timeout exists to end a task
    wedged in PROVISIONING, not to accommodate one. Long enough that the largest
    legitimate population is never cut off, short enough that a stuck task does
    not hold an execution open for the full 3600.
  EOT
  type        = number
  default     = 1800
}

variable "pipeline_execution_timeout" {
  description = <<-EOT
    Seconds an execution may run in total. A Standard workflow's default is a
    year, which is the wrong default for a question somebody is waiting on: an
    execution that hangs should end and say so.

    3600, raised from 1800, to stay clear of the two 1800-second branch timeouts
    beneath it. The old pair was 1800 and 900 -- an execution ceiling exactly
    twice a branch ceiling, which is fine until two branches run in sequence
    around the fan-out. Keeping this at twice pipeline_task_timeout preserves the
    property that a branch timeout always fires first and names the branch, rather
    than the execution timing out and naming nothing.
  EOT
  type        = number
  default     = 3600
}

variable "pipeline_log_retention_days" {
  description = "CloudWatch Logs retention for the state machine's transition log. Shorter than the 90-day execution history on purpose: the history is the audit trail, this group is for watching a run go past."
  type        = number
  default     = 14
}

variable "alert_email" {
  description = <<-EOT
    Address the alarm topic mails. Empty by default, which creates the topic and
    the alarms but no subscription, so the build still applies for anyone who does
    not want mail. Set it in terraform.tfvars, which is gitignored -- the repo is
    public and this is a personal address.

    An email subscription is created in PendingConfirmation and delivers nothing
    until the link in the confirmation mail is clicked. Terraform reports the
    resource as created either way and cannot see the difference, so confirm it
    once by hand:
      aws sns list-subscriptions-by-topic --topic-arn <topic arn>
    SubscriptionArn reads PendingConfirmation before the click and a real ARN after.
  EOT
  type        = string
  default     = ""
}

variable "pipeline_slow_threshold_ms" {
  description = <<-EOT
    Execution time above which an otherwise successful run raises an alarm.

    600,000 ms against an observed 115-131 seconds end to end, so roughly five
    times the normal figure. The gap is deliberate and it is mostly Fargate: two
    branches start cold, and a task sitting in PROVISIONING for a few minutes is
    slow rather than broken. Tightening this toward the observed time would alarm
    on a cold start, which is the normal case here, not a fault.

    The point of this alarm is the failure the other two cannot see -- a run that
    succeeds but takes ten minutes is still ExecutionsSucceeded = 1 and never
    reaches pipeline_execution_timeout, so without this it is invisible until an
    analyst complains.
  EOT
  type        = number
  default     = 600000
}

variable "rds_low_memory_threshold_bytes" {
  description = <<-EOT
    FreeableMemory floor, in bytes. 41,943,040 -- 40 MiB.

    Measured rather than chosen. Six hours of the live db.t4g.micro reported
    FreeableMemory between 86.8 and 146.8 MiB, averaging 101.7, so this instance
    genuinely runs with well under a tenth of its 1 GiB free and that is its
    normal state, not a warning. A threshold at 100 MiB -- which is what the
    round-number instinct suggests -- would have alarmed continuously from the
    moment it was applied.

    40 MiB sits below the observed floor with room to spare, so it fires on
    something that has never happened rather than on Tuesday. Paired with three
    evaluation periods, because a single dip during a large scan is the instance
    working.

    Worth watching for a second reason: 1 GiB of RAM against a working set of
    roughly 412 MB is a small page cache, which is exactly why a table scan
    straight after a restart is slow -- the condition that made the pg8000 socket
    timeout in Phase 12 look intermittent.
  EOT
  type        = number
  default     = 41943040
}
