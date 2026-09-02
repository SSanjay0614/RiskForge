# RiskForge on AWS — Native Rebuild

RiskForge began as a local Python application: a Streamlit front end, two XGBoost
models, a SQLite database and an agent loop that turned analyst questions into
SQL. This document describes the AWS-native rebuild of that system — the same
analytical behaviour, re-expressed entirely in managed services, defined in
Terraform, and running in `us-east-1`.

Nothing about the models or the risk mathematics changed. What changed is that
every stage now runs as a separate, independently scalable AWS resource, with the
orchestration, audit trail and network boundaries that a real credit-risk
platform needs.

## What it does

An analyst asks a question in plain English — *"what is the expected loss on the
California book?"* The platform decides whether the question is answerable,
writes the SQL itself, runs it against a PostgreSQL portfolio of 878,317 loans,
scores every returned loan through calibrated probability-of-default and
loss-given-default models, measures interest-rate and concentration risk, checks
the result against regulatory limits, and returns an aggregate risk report.

Raw borrower rows are never rendered and never reach a language model.

## Architecture

```
                        ┌──────────────────────────┐
   Analyst ──HTTPS──▶   │  EC2 · Streamlit UI      │
                        └────────────┬─────────────┘
                                     │ StartExecution
                        ┌────────────▼─────────────┐
                        │  Step Functions          │
                        │  riskforge-pipeline      │
                        └────────────┬─────────────┘
             ┌───────────────────────┼───────────────────────┐
             │                       │                       │
      ┌──────▼──────┐        ┌───────▼───────┐       ┌───────▼───────┐
      │  Lambda ×4  │        │  Lambda       │       │  Lambda ×2    │
      │  Guard      │        │  ExecuteSQL   │       │  ScoreLoans   │
      │  GenerateSQL│        │               │       │  MeasureRates │
      │  Evaluate   │        └───────┬───────┘       └───┬───────┬───┘
      │  Compliance │                │                   │       │
      └──────┬──────┘                │                   │       │
             │                 ┌─────▼─────┐             │       │
      ┌──────▼──────┐          │   RDS     │             │  ┌────▼─────────┐
      │ Gemini API  │          │ PostgreSQL│◀────────────┘  │  SageMaker   │
      │ (generative)│          └─────┬─────┘                │  PD + LGD    │
      └─────────────┘                │                      │  (fallback)  │
                                     ▼                      └──────────────┘
                              ┌─────────────┐
                              │  S3         │
                              │  artifacts  │
                              └─────────────┘
```

## The pipeline

A single Step Functions state machine owns the whole flow. Every transition is
recorded, every failure has a named terminal state, and the execution history is
the audit trail.

| State | Runs on | Purpose |
|---|---|---|
| `Guard` | Lambda | Decides whether the question is answerable from the schema |
| `GenerateSQL` | Lambda | Writes PostgreSQL from the question and schema |
| `ExecuteSQL` | Lambda | Runs the query, streams the result to S3 |
| `BuildProfile` | Pass | Reduces the result to a shape-only profile |
| `Evaluate` | Lambda | Judges the query against the profile, not the rows |
| `FanOut` | Parallel | Runs credit risk and rate risk concurrently |
| `ScoreLoans` | Lambda (image) | Feature engineering, then PD and LGD inference |
| `MeasureRates` | Lambda (image) | Repricing gap and concentration metrics |
| `Compliance` | Lambda | Checks the aggregates against `risk_limits` |
| `Summarise` | Pass | Assembles the response |

Retry paths exist for query generation and evaluation: a failed query returns its
error to the generator, and a rejected query returns the evaluator's feedback,
each with a bounded number of attempts before a named failure state.

## Services in use

| Service | Resource | Configuration |
|---|---|---|
| Step Functions | `riskforge-pipeline` | Standard workflow, 36 states, CloudWatch logging |
| Lambda | 7 functions | 5 zips on arm64 (256 MB agents, 10240 MB / 15 min for SQL); 2 container images on x86_64 (10240 and 4096 MB) |
| ECS Fargate | `riskforge-risk:2` | Retained as the rollback path, not on the live pipeline |
| SageMaker | 2 endpoints | Serverless Inference, 3072 MB, XGBoost 3.0-5; the fallback scoring transport |
| RDS | `riskforge-db` | PostgreSQL 16.15, `db.t4g.micro`, gp3 20 GB / 3000 IOPS |
| S3 | `riskforge-artifacts-<account>` | Query results, risk results, model bundles |
| ECR | `riskforge-risk` | The risk image, run by both Lambda and ECS |
| EC2 | `t3.small` | Streamlit front end |
| CloudWatch | 5 alarms + 9 declared log groups | Failure, latency, timeout, throttle, DB memory |
| SNS | `riskforge-alerts` | Alarm delivery |
| Systems Manager | Parameter Store | API key, as a `SecureString` |
| Secrets Manager | RDS master secret | Managed by RDS, never in Terraform source |

## Data tier

The portfolio moved from a single SQLite file to RDS PostgreSQL, normalised into
three tables: `loans` (12 columns, the contract), `borrowers` (25 columns, the
credit profile) and `risk_limits` (5 rows of policy thresholds). 878,317 rows in
each of the first two, verified row-for-row against the source after migration.

Queries return through `COPY … TO STDOUT`, streamed directly from the database
cursor to a multipart S3 upload. The Lambda never materialises the result set, so
the whole 878,317-row book extracts in around 25 seconds using 130 MB of memory.

Results land in S3 under a date-partitioned prefix; only a small inline preview
and a row-count profile travel through the state machine itself.

## Model serving

The two models are served as SageMaker Serverless Inference endpoints, built from
the notebook artifacts by `Deploy/sagemaker/build_artifacts.py` and packaged with
a custom `inference.py` handler running in framework mode on the prebuilt
XGBoost 3.0-5 image.

Serverless rather than real-time is deliberate: the endpoints bill per request
and per millisecond of duration, and nothing at all while idle, which suits a
workload that runs in bursts.

Feature alignment is enforced twice — once when the risk function builds the
frame, once inside the model handler — and `verify_endpoints.py` replays fixed
reference rows against the live endpoints at zero tolerance, so a prediction from
AWS is provably identical to a prediction from the notebook.

Scoring itself runs in-process by default. `SCORING_MODE=local` loads the PD and
LGD boosters inside the risk function and calls the endpoints' own `inference.py`
handler directly; `SCORING_MODE=endpoint` sends the same batches over SageMaker
Runtime instead. That is one environment variable rather than two
implementations: `stage.py` extracts the booster, manifest and calibration out of
the same `model.tar.gz` bundles the endpoints serve, and asserts the packaged
`code/inference.py` is byte-identical to `Deploy/sagemaker/inference.py`, so the
two modes differ in transport only. `verify_local_scoring.py` replays reference
rows through both and requires a maximum absolute difference of zero — it
reports `0.000e+00` on both models.

The reason for the default is round trips, not per-request speed. ModelLatency
measured 78-177 ms, which is fine; but 878,317 rows batched across two models is
roughly 880 HTTPS requests, and at five concurrent that is about 176 sequential
waves, each paying an OverheadLatency of 5.4-6.6 seconds when cold. The output
envelope records which mode produced the numbers, so a report always says how it
was scored.

## Compute

Five Lambda functions cover everything that is short and I/O-bound. The three
generative steps call the Gemini API through a shared client module; the SQL
executor is sized for network throughput rather than memory; the compliance check
is pure arithmetic against the limits table.

The two analytical stages need pandas, the full feature-engineering pipeline and
several gigabytes of headroom, so they run as **container-image** Lambda
functions — the same image, tag and digest the ECS task definition still points
at, dispatched by `entry.sh` on the presence of `AWS_LAMBDA_RUNTIME_API`. Neither
has a default mode: Step Functions supplies `score` or `rates` in the payload, so
a misconfigured state fails loudly instead of running the wrong branch.

They began as Fargate tasks, and the reason they moved is measured rather than
assumed — see the Latency section. Lambda caches the image layers instead of
pulling them per invocation, keeps the execution environment warm between
questions, and returns the moment the handler returns.

Neither risk function is attached to the VPC. Both reach S3 and — in endpoint
mode — SageMaker Runtime, which are public AWS API endpoints reached over TLS
with SigV4; the Fargate task they replace ran with a public IP on a public
subnet, so this is the same posture with one fewer ENI. What bounds them is IAM:
one bucket prefix to read, a different one to write, two endpoint ARNs to invoke,
and no database, no secret and no Parameter Store. Attaching them to the VPC
would additionally break endpoint mode, because there is no SageMaker Runtime
interface endpoint and no NAT gateway.

`infra/ecs.tf`, the task definition and the `ecs:RunTask` grant are all still in
place, so moving back is an edit to `pipeline.asl.json` and an apply — not a
rebuild, because one image serves both runtimes.

The image reuses `tools/feature_engineering_tool.py` from the repository
unchanged. The AWS path and the local path are the same code, which is what makes
their numbers comparable.

## Latency

The whole-portfolio question — 878,317 rows, every one of them scored — is the
slowest thing the platform does and the number the design is tuned against. On
the Fargate architecture it took 209.5 seconds end to end, and the Step Functions
execution history said where the time went rather than leaving it to be guessed:

- **21-49 seconds per branch before any application code ran.** Across five task
  lifecycles: 4-23s attaching an ENI, 8-17s pulling the 150 MB image, 3s
  starting.
- **24-29 seconds per branch after the answer was already in S3**, because
  `ecs:runTask.sync` waits for `STOPPED` — 10.0s of SIGTERM grace plus 14-19s of
  deregistration.
- **~880 HTTPS round trips** to the two model endpoints, about 176 sequential
  waves at five concurrent.

`MeasureRates` spent 52.8 seconds on 3.2 seconds of arithmetic — 94% overhead.
`ScoreLoans` spent 116.6 seconds on 40.2 seconds of work. None of that is Fargate
being slow; it is a per-request container lifecycle being the wrong shape for a
request.

Three changes address exactly those three lines, and none of them touches the
risk mathematics:

| Change | What it removes |
|---|---|
| Fargate → container-image Lambda | Per-request ENI attach, image pull and deregistration — 45-78s across the two branches |
| In-process scoring (`SCORING_MODE=local`) | ~880 sequential HTTPS round trips and their cold-start overhead |
| 16 MiB parts, concurrent part uploads, 10240 MB on `ExecuteSQL` | The strict read/write alternation in the S3 sink, and CPU on pg8000's single-threaded framing |

Measured after deployment, on that same question, the pipeline now runs in **60.8
seconds** end to end -- 59.4 of it inside the state machine:

| State | Duration | Peak memory |
|---|---|---|
| `Guard` | 7.12s | |
| `GenerateSQL` | 1.46s | |
| `ExecuteSQL` | 22.86s | 152 MB of 10240 |
| `Evaluate` | 1.64s | |
| `FanOut` | **24.87s** | |
| &nbsp;&nbsp;`ScoreLoans` | 24.71s | 2442 MB of 10240 |
| &nbsp;&nbsp;`MeasureRates` | 8.98s | 1427 MB of 4096 |
| `Compliance` | 1.34s | 103 MB of 256 |

The fan-out is the line the change set was aimed at, and it is the line that moved:
116.6 seconds became 24.87, and `MeasureRates` went from 52.8 seconds wrapped around
3.2 seconds of arithmetic to 8.98 seconds wrapped around 8.8. The peak-memory column
is why the allocations are what they are rather than an oversight -- Lambda scales
CPU with memory, so 10240 MB on the scoring branch is buying about six vCPUs for the
feature-engineering pass, not headroom it never touches.

`ExecuteSQL` is now the largest single state, and its own `REPORT` line puts that
time in the query rather than the function: 634 ms of init and 152 MB of 10240 MB
used, because the result streams to S3 and is never materialised.

The database is not the constraint, and establishing that took a measurement rather
than an assumption: `work_mem` was raised from the engine default of 4 MB to 32 MB
with `hash_mem_multiplier` at 2.0, and the extract moved from 22731.72 ms to
22732.57 ms. Under a millisecond apart, because both tables were already in the
buffer cache. So the 177 MB CSV leaves the database at roughly 7.8 MB/s for
client-side reasons -- one Python thread decoding a `COPY ... TO STDOUT` stream,
which no amount of Lambda memory past the first vCPU can widen.

A larger instance is not available to try: `db.t4g.medium` was attempted and RDS
refused it with `FreeTierRestrictionError`, so 1 GiB is a ceiling on this account
rather than a sizing decision, and two full-portfolio runs draw CPUCreditBalance
from full to zero.

One cliff remains, and it is a cold-start cliff rather than a steady-state one. Both
risk functions load pandas, xgboost and two boosters at module scope, which exceeds
Lambda's 10-second init budget -- `MeasureRates` measures 27.8s cold against 0.69
warm and `ScoreLoans` 40.2 against 5.0 -- and provisioned concurrency, which would
remove it, is blocked by Service Quotas `L-B99A9384`. So the first question after an
idle period pays it and every question after does not.

**The full measurement record is in
[AWS_LATENCY_TUNING.md](AWS_LATENCY_TUNING.md)** -- where the 209.5 seconds went
line by line, what each of the three changes recovered, how the two scoring modes
are proved identical to zero tolerance, the measurements that came back null, and
the options considered and rejected for this account.

## Security posture

- RDS has no public address, is encrypted at rest, and enforces TLS on every
  connection. The CA bundle is fetched at build time and certificates are
  verified, not trusted blindly.
- Database credentials live in an RDS-managed Secrets Manager secret and the API
  key in Parameter Store as a `SecureString`. Neither appears in Terraform
  source or in an environment variable in the repository.
- Security groups are referenced by group, not by CIDR: the database accepts
  traffic from the Lambda and application groups only.
- IAM is per-role and per-resource. Each Lambda, the ECS task, the ECS execution
  role, SageMaker and Step Functions each hold their own policy, scoped to the
  specific bucket prefixes, secrets, endpoints and log groups they use. The two
  risk functions have separate roles rather than a shared one, because only the
  scoring branch has a code path that calls SageMaker. Neither may create a log
  group, so a name drift fails to log instead of quietly opening a second one
  that never expires.
- S3 blocks all public access, is encrypted with AES256, and a lifecycle rule
  expires query results after 7 days so extracted portfolio data does not linger.
- The language model sees the schema and a row-count profile. It never sees a
  borrower row. The user interface renders aggregates only.

## Observability

Five CloudWatch alarms publish to an SNS topic: pipeline failure, pipeline
timeout, pipeline latency above threshold, Lambda throttling, and low freeable
memory on the database. Every Lambda, the retained ECS task, both SageMaker
endpoints, the state machine and PostgreSQL itself write to their own log group.

Step Functions execution history gives per-state timings, so the cost of any
stage is a query rather than a guess. Since the risk stages became Lambda
functions, each one's `REPORT` line also states its own billed duration and peak
memory — which the Fargate task never did, because Container Insights is
disabled.

## Infrastructure as code

The whole stack is Terraform — 26 configuration files, no console clicks. IAM is
split one file per role so a policy change is reviewable in isolation.

```
infra/
  providers.tf versions.tf variables.tf data.tf outputs.tf
  vpc_endpoints.tf sg_app.tf sg_db.tf
  rds.tf s3.tf ecr.tf ec2.tf
  lambda.tf lambda_agents.tf lambda_risk.tf ecs.tf sagemaker.tf stepfunctions.tf
  cloudwatch.tf
  iam_lambda.tf iam_lambda_agents.tf iam_lambda_risk.tf iam_ecs.tf
  iam_sagemaker.tf iam_sfn.tf iam_ec2.tf
```

Deployment artifacts are built by scripts rather than committed:

```
Deploy/
  lambda/         build.py packages each function; shared/ is vendored in
  fargate/        stage.py assembles the build context and extracts the model
                  artifacts, Dockerfile, entry.sh, build_and_push.py,
                  verify_local_scoring.py
  sagemaker/      build_artifacts.py, inference.py, verify_endpoints.py
  stepfunctions/  pipeline.asl.json, run_pipeline.py
  streamlit/      bootstrap.sh for the EC2 front end
Database/migration/  schema, finalize, verify, run_migration.sh
```

## Running it

Artifacts are built before the apply, not after it. Two resources refuse to
create without them: `aws_lambda_function` reads each zip off disk to compute
`source_code_hash`, and a `package_type = "Image"` function fails outright if the
tag is not already in ECR.

```bash
python Deploy/lambda/build.py               # package the five zip functions
python Deploy/sagemaker/build_artifacts.py  # PD and LGD model.tar.gz bundles

python Deploy/fargate/stage.py              # assemble the build context, extract artifacts
python Deploy/fargate/verify_local_scoring.py   # local == endpoint, zero tolerance
python Deploy/fargate/build_and_push.py     # build and push the risk image to ECR

cd infra
terraform init
terraform apply

bash  ../Database/migration/run_migration.sh
python ../Deploy/sagemaker/verify_endpoints.py
python ../Deploy/stepfunctions/run_pipeline.py "expected loss on the California book"
```

`terraform output` lists the endpoint names, bucket, state machine ARN and the
front-end address.
