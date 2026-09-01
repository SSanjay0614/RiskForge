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
      │  Lambda ×4  │        │  Lambda       │       │  Fargate ×2   │
      │  Guard      │        │  ExecuteSQL   │       │  ScoreLoans   │
      │  GenerateSQL│        │               │       │  MeasureRates │
      │  Evaluate   │        └───────┬───────┘       └───┬───────┬───┘
      │  Compliance │                │                   │       │
      └──────┬──────┘                │                   │       │
             │                 ┌─────▼─────┐             │       │
      ┌──────▼──────┐          │   RDS     │             │  ┌────▼─────────┐
      │ Gemini API  │          │ PostgreSQL│◀────────────┘  │  SageMaker   │
      │ (generative)│          └─────┬─────┘                │  PD + LGD    │
      └─────────────┘                │                      │  Serverless  │
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
| `ScoreLoans` | Fargate | Feature engineering, then PD and LGD inference |
| `MeasureRates` | Fargate | Repricing gap and concentration metrics |
| `Compliance` | Lambda | Checks the aggregates against `risk_limits` |
| `Summarise` | Pass | Assembles the response |

Retry paths exist for query generation and evaluation: a failed query returns its
error to the generator, and a rejected query returns the evaluator's feedback,
each with a bounded number of attempts before a named failure state.

## Services in use

| Service | Resource | Configuration |
|---|---|---|
| Step Functions | `riskforge-pipeline` | Standard workflow, 36 states, CloudWatch logging |
| Lambda | 5 functions | Python 3.12; 256 MB agents, 3008 MB / 15 min for SQL |
| ECS Fargate | `riskforge-risk:2` | 4 vCPU / 30 GB, one image, two modes |
| SageMaker | 2 endpoints | Serverless Inference, 3072 MB, XGBoost 3.0-5 |
| RDS | `riskforge-db` | PostgreSQL 16.15, `db.t4g.micro`, gp3 20 GB / 3000 IOPS |
| S3 | `riskforge-artifacts-<account>` | Query results, risk results, model bundles |
| ECR | `riskforge-risk` | The Fargate image |
| EC2 | `t3.small` | Streamlit front end |
| CloudWatch | 5 alarms + 10 log groups | Failure, latency, timeout, throttle, DB memory |
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

Feature alignment is enforced twice — once when the Fargate task builds the
frame, once inside the container handler — and `verify_endpoints.py` replays
fixed reference rows against the live endpoints at zero tolerance, so a
prediction from AWS is provably identical to a prediction from the notebook.

## Compute

Five Lambda functions cover everything that is short and I/O-bound. The three
generative steps call the Gemini API through a shared client module; the SQL
executor is sized for network throughput rather than memory; the compliance check
is pure arithmetic against the limits table.

The two analytical stages run on Fargate because they need pandas, the full
feature-engineering pipeline and headroom that a 15-minute function cannot
guarantee. Both are the *same* container image with no default mode — Step
Functions supplies `--mode score` or `--mode rates` as a command override, so a
misconfigured state fails loudly instead of running the wrong branch.

The image reuses `tools/feature_engineering_tool.py` from the repository
unchanged. The AWS path and the local path are the same code, which is what makes
their numbers comparable.

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
  specific bucket prefixes, secrets, endpoints and log groups they use.
- S3 blocks all public access, is encrypted with AES256, and a lifecycle rule
  expires query results after 7 days so extracted portfolio data does not linger.
- The language model sees the schema and a row-count profile. It never sees a
  borrower row. The user interface renders aggregates only.

## Observability

Five CloudWatch alarms publish to an SNS topic: pipeline failure, pipeline
timeout, pipeline latency above threshold, Lambda throttling, and low freeable
memory on the database. Every Lambda, the Fargate task, both SageMaker endpoints,
the state machine and PostgreSQL itself write to their own log group.

Step Functions execution history gives per-state timings, so the cost of any
stage is a query rather than a guess.

## Infrastructure as code

The whole stack is Terraform — 24 configuration files, no console clicks. IAM is
split one file per role so a policy change is reviewable in isolation.

```
infra/
  providers.tf versions.tf variables.tf data.tf outputs.tf
  vpc_endpoints.tf sg_app.tf sg_db.tf
  rds.tf s3.tf ecr.tf ec2.tf
  lambda.tf lambda_agents.tf ecs.tf sagemaker.tf stepfunctions.tf
  cloudwatch.tf
  iam_lambda.tf iam_lambda_agents.tf iam_ecs.tf iam_sagemaker.tf
  iam_sfn.tf iam_ec2.tf
```

Deployment artifacts are built by scripts rather than committed:

```
Deploy/
  lambda/         build.py packages each function; shared/ is vendored in
  fargate/        stage.py assembles the build context, Dockerfile, build_and_push.py
  sagemaker/      build_artifacts.py, inference.py, verify_endpoints.py
  stepfunctions/  pipeline.asl.json, run_pipeline.py
  streamlit/      bootstrap.sh for the EC2 front end
Database/migration/  schema, finalize, verify, run_migration.sh
```

## Running it

```bash
cd infra
terraform init
terraform apply

python ../Deploy/lambda/build.py            # package and publish the functions
python ../Deploy/sagemaker/build_artifacts.py
python ../Deploy/fargate/build_and_push.py  # build and push to ECR
bash  ../Database/migration/run_migration.sh
python ../Deploy/sagemaker/verify_endpoints.py
python ../Deploy/stepfunctions/run_pipeline.py "expected loss on the California book"
```

`terraform output` lists the endpoint names, bucket, state machine ARN and the
front-end address.
