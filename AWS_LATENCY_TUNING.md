# RiskForge on AWS — Latency

The whole-portfolio question — 878,317 loans, every one of them extracted from
PostgreSQL and scored through calibrated PD and LGD models — is the slowest thing
the platform does. It is the number the design is tuned against, because anything
narrower is a subset of it.

**It went from 209.5 seconds to 60.8 seconds.** Nothing about the models or the
risk mathematics changed to get there. The pipeline computes the same expected
loss from the same boosters against the same 878,317 rows; what changed is the
compute each stage runs on and how many network round trips sit between them.

This document is the measurement record: where the 209.5 seconds actually went,
which three changes removed which parts of it, what was verified afterwards, and
what is now the floor. The architecture itself is described in
[AWS_NATIVE_REBUILD.md](AWS_NATIVE_REBUILD.md).

## Where the 209.5 seconds went

The first thing worth saying is that none of it was guessed. A Step Functions
execution history records an enter and exit timestamp for every state, and each
Lambda and ECS task logs its own duration, so the breakdown below is subtraction
rather than inference — and it is the reason the change set is three items long
instead of a general attempt to make things faster.

On the original architecture the two risk stages ran as ECS Fargate tasks,
dispatched by `ecs:runTask.sync`. Measured across five task lifecycles:

| Where the time went | Cost | Application code involved |
|---|---|---|
| ENI attach | 4–23s per task | none |
| Image pull (150 MB) | 8–17s per task | none |
| Container start | ~3s per task | none |
| `runTask.sync` waiting for `STOPPED` | 24–29s per task (10.0s SIGTERM grace + 14–19s deregistration) | none — the result was already in S3 |
| ~880 HTTPS requests to two SageMaker endpoints | ~176 sequential waves at five concurrent | yes, but as transport |

Two numbers make the shape of the problem obvious:

- **`MeasureRates` occupied 52.8 seconds to do 3.2 seconds of arithmetic** — 94%
  overhead. Repricing gap and a Herfindahl index over a frame already in memory
  is not slow; it never got the chance to be.
- **`ScoreLoans` occupied 116.6 seconds to do 40.2 seconds of work.**

None of that is Fargate being slow. It is a per-request container lifecycle being
the wrong shape for a request: the platform was paying for a machine to be
created and destroyed around work measured in seconds, twice, on every question.

## The three changes

Each one targets a specific line of the table above, and none of them touches the
risk mathematics.

| Change | What it removes | Recovered |
|---|---|---|
| Fargate → container-image Lambda | Per-request ENI attach, image pull and deregistration | 45–78s across the two branches |
| In-process scoring (`SCORING_MODE=local`) | ~880 sequential HTTPS round trips and their cold-start overhead | the bulk of `ScoreLoans`' remaining time |
| 16 MiB parts, concurrent part uploads, 10240 MB on `ExecuteSQL` | Strict read/write alternation in the S3 sink, and CPU starvation on pg8000's single-threaded framing | the extract's own overhead |

### 1. Container-image Lambda instead of Fargate

The same image, the same tag, the same digest the ECS task definition still
points at. `entry.sh` dispatches on the presence of `AWS_LAMBDA_RUNTIME_API`, so
one image serves both runtimes and there is no second thing to keep in step.

Lambda caches the image layers instead of pulling them per invocation, keeps the
execution environment warm between questions, and returns the moment the handler
returns — there is no teardown for the caller to wait through. Neither risk
function is VPC-attached, so there is no ENI to attach either: both reach only S3
and, in endpoint mode, SageMaker Runtime, which are public AWS API endpoints over
TLS with SigV4. The Fargate task they replace ran with a public IP on a public
subnet, so this is the same network posture with one fewer ENI, and what bounds
them is IAM — one bucket prefix to read, a different one to write, two endpoint
ARNs to invoke, and no database, no secret and no Parameter Store.

Neither function has a default mode. Step Functions supplies `score` or `rates`
in the payload, so a misconfigured state fails loudly instead of running the
wrong branch and producing a valid-looking result with the wrong numbers in it.

### 2. Scoring in-process instead of over the network

`SCORING_MODE=local` loads the PD and LGD boosters inside the risk function and
calls the endpoints' own `inference.py` handler directly. `SCORING_MODE=endpoint`
sends the same batches over SageMaker Runtime. That is one environment variable
rather than two implementations: `stage.py` extracts the booster, manifest and
calibration out of the same `model.tar.gz` bundles the endpoints serve, and
asserts the packaged `code/inference.py` is byte-identical to
`Deploy/sagemaker/inference.py` — so the two modes differ in transport only.

**The reason for the change is round trips, not per-request speed.**
`ModelLatency` measured 78–177 ms, which is entirely reasonable. But 878,317 rows
batched across two models is roughly 880 HTTPS requests, and at five concurrent
that is about 176 sequential waves, each paying an `OverheadLatency` of 5.4–6.6
seconds when cold. The endpoints were never the problem; asking them 880 times
in sequence was.

Both endpoints are still deployed and still verified. Endpoint mode is the
fallback that needs no rebuild, and the output envelope records which mode
produced the numbers, so a report always says how it was scored.

### 3. Widening the extract

`ExecuteSQL` streams `COPY … TO STDOUT` from the database cursor straight into a
multipart S3 upload, so the result set is never materialised in the function.
Three adjustments: parts doubled from 8 MiB to 16 MiB (177 MB is 23 parts at 8
MiB and 12 at 16), part uploads moved onto a four-worker pool so a part upload no
longer blocks the next socket read, and memory raised to 10240 MB — which on
Lambda buys CPU, not headroom, since CPU scales with the memory allocation.

The peak-memory column below is why that allocation is what it is rather than an
oversight: the function uses 152 MB of 10240.

## What was verified, and how

A latency change that quietly alters a number is not an optimisation, it is a
bug with a stopwatch attached. Two checks stand between the two scoring modes:

- `Deploy/fargate/stage.py` asserts the `code/inference.py` inside each
  `model.tar.gz` is byte-identical to `Deploy/sagemaker/inference.py`. If the
  packaged handler and the repository handler ever diverge, the build fails
  rather than the results.
- `Deploy/fargate/verify_local_scoring.py` replays fixed reference rows through
  both transports and requires a maximum absolute difference of **zero**. It
  reports `0.000e+00` on both PD and LGD.

Feature alignment is enforced twice independently — once when the risk function
builds the frame, once inside the model handler — and the image reuses
`tools/feature_engineering_tool.py` from the repository unchanged, which is what
makes the AWS numbers and the local numbers comparable at all.

Rollback was kept rather than described. `infra/ecs.tf`, the task definition and
the `ecs:RunTask` grant are all still in place, so returning to Fargate is an
edit to `pipeline.asl.json` and an apply — not a rebuild, because one image
already serves both runtimes.

## The 60.8 seconds, measured

Same question, same 878,317 rows, after deployment. 59.4 seconds of it inside the
state machine; the remaining 1.4 is `StartExecution` and the front end.

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

The fan-out is the line the change set was aimed at and it is the line that
moved: **116.6 seconds became 24.87**, and `MeasureRates` went from 52.8 seconds
wrapped around 3.2 seconds of arithmetic to 8.98 seconds wrapped around 8.8.

One structural detail is worth reading off this table before doing any further
work on it. `FanOut` costs `max(24.71, 8.98)`, so the rates branch has **15.9
seconds of slack** — every second removed from it is invisible at the top level
until it exceeds the scoring branch. The critical path is `Guard`, `ExecuteSQL`
and `ScoreLoans`, and those three are 54.7 of the 59.4.

## A measurement that came back null

`ExecuteSQL` is now the largest single state, and the obvious suspicion is the
database. Establishing that it is not took a measurement rather than an
assumption, and the result was negative — which is recorded here for the same
reason it is recorded in `infra/rds.tf`: a null result that goes unwritten gets
re-derived by the next person.

`work_mem` is unset in the `postgres16` parameter family, which means the engine
default of 4 MB against a hash join over 878,317 rows a side. It was raised to 32
MB with `hash_mem_multiplier` at 2.0, and the extract went from **22731.72 ms to
22732.57 ms**. Under a millisecond apart.

The instance was at 38 read IOPS, 3 MB/s and 14% CPU while it ran. Nothing was
spilling, because both tables were already in the buffer cache. The parameters
were kept anyway, because the case they address is real — a first query of a
session against a cold cache measured **46.2 seconds** behind 340 read IOPS — but
on the warm path they are a null result rather than an implied win.

## The floor

So the 177 MB CSV leaves the database at roughly **7.8 MB/s for client-side
reasons**: one Python thread decoding a `COPY … TO STDOUT` stream. pg8000 is pure
Python by choice, because psycopg2 is a C extension that cannot be packaged from
the development machine, and no amount of Lambda memory past the first vCPU
widens a single thread.

The instance size is a ceiling rather than a decision. `db.t4g.medium` was
attempted and RDS refused it — `FreeTierRestrictionError: This instance size
isn't available with free plan accounts` — so 1 GiB is what the account permits,
and the two full-portfolio runs above draw `CPUCreditBalance` from full to zero.
That is not a throttle while it lasts, but it does bound how many questions in a
row hold this speed.

## The cold-start cliff

One cliff remains, and it is a cold-start cliff rather than a steady-state one.
Both risk functions import pandas, xgboost and scipy and load two boosters at
module scope, which exceeds Lambda's 10-second init budget: `INIT_REPORT` reports
`Status: timeout` and Lambda re-runs the initialisation inside the first
invocation. On an identical 38,648-row input:

| Branch | Cold | Warm |
|---|---|---|
| `MeasureRates` | 27.8s | 0.69s |
| `ScoreLoans` | 40.2s | 5.0s |

PostgreSQL's buffer cache is cold at the same time, which is the 46.2s extract
above rather than 22.9s.

Provisioned concurrency would remove all of it and cannot be used: Service Quotas
`L-B99A9384` is 10 on this account and Lambda holds all 10 back as the unreserved
minimum. So the first question after an idle period pays it and every question
after does not, which makes one throwaway question the operational answer.

## What is not done, and why it is named here

Sub-40 seconds is reachable and is not claimed. The candidates, with the reason
each is still a candidate:

- **A projection tighter than `SELECT *` across 37 columns.** Pays twice — fewer
  bytes to stream out of the database and fewer to parse in the scoring branch.
  The cost is that it constrains what the SQL generator may project, which is a
  behavioural change to a model-written query rather than a code change.
- **A C driver in place of pg8000.** Attacks the constant factor on the thread
  that bounds the extract rather than trying to widen it. `Deploy/lambda/build.py`
  already names the packaging route — a manylinux `aarch64` wheel pulled with
  `--platform` flags.
- **A multithreaded CSV parse in the risk image.** `inputs._read_csv` uses
  pandas' single-threaded parser with `low_memory=False` on 878,317 × 37, on a
  function that has roughly six vCPUs. The NA semantics there are deliberate and
  load-bearing — a borrower whose `emp_title` is literally `"NA"` must stay a
  string — so any replacement has to preserve them, which
  `verify_local_scoring.py` is already able to prove.

Two options were considered and rejected on this account specifically:

- **Splitting the extract into concurrent range queries.** Range-splitting
  model-generated SQL means wrapping it per worker, which re-runs the inner hash
  join once per worker. That trades client CPU, which is plentiful, for database
  CPU on a `db.t4g.micro` whose credit balance is already the binding constraint.
- **Streaming the extract into a Distributed Map so scoring overlaps it.**
  Theoretically the largest single win, since `ExecuteSQL` and `FanOut` are 47.7
  seconds of strictly serial time. But every Map branch is its own Lambda
  execution environment and both risk functions exceed the init budget, so a
  four-way Map means four cold initialisations rather than one. It makes the warm
  path faster and the first-question path considerably worse, and provisioned
  concurrency — the thing that would fix it — is quota-blocked.

Both are recorded because the reason they were rejected is a property of this
account, not of the design. On an account without the free-plan instance ceiling
and with concurrency headroom, the second one is where the next 18 seconds are.
