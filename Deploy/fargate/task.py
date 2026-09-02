"""
The container's entrypoint. One image, two modes.

    python task.py --mode score
        --source-uri s3://.../query-results/<id>.csv
        --output-uri s3://.../risk-results/<run>/credit.json
    python task.py --mode rates
        --source-uri s3://.../query-results/<id>.csv
        --output-uri s3://.../risk-results/<run>/rates.json

`score` is agents/credit_risk_agent.py and `rates` is
agents/interest_rate_concentration_agent.py. Both read the same query result CSV
and neither reads the other's output, which is what makes them a fan-out: the
Step Functions Parallel state in Phase 11 starts two tasks from this one image at
the same time and joins their two S3 objects afterwards.

**Why one image and not two.** The two modes share the reader, the output guard,
the frequency maps and the pinned pandas -- the differences are which tools run.
Two images would be two builds, two ECR repositories, two things to keep at the
same pandas version, and the shared 90% would drift. What a single image costs is
that the rates task carries scoring code it never calls, which is a few kilobytes
of Python.

**Why S3 and not stdout.** A Fargate task returns an exit code to Step Functions
and nothing else -- there is no return value, and stdout goes to CloudWatch Logs
where a state machine cannot read it. So each branch writes its aggregates to the
key it was given, and the state machine passes those two keys forward.

There is no default mode. A default would let a misconfigured state silently run
the wrong branch and produce a valid-looking JSON with the wrong numbers in it;
missing arguments exit 2 with the usage instead.
"""
import argparse
import datetime as dt
import os
import sys
import traceback

# The repository modules live at /app alongside this file (see stage.py), so the
# import path is already right in the container. This makes it right when the
# task is run from a checkout for testing, where task.py is in Deploy/fargate/
# and the repository root is two levels up.
if not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")):
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))

from riskforge import credit, inputs, outputs, rates  # noqa: E402
from utils.logger import logger  # noqa: E402

MODES = ("score", "rates")
SCORING_MODES = ("local", "endpoint")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="task.py",
        description="RiskForge risk computation task: Expected Loss, or interest "
                    "rate risk and concentration.",
    )
    parser.add_argument(
        "--mode", choices=MODES, default=os.environ.get("RISKFORGE_MODE"),
        help="score = PD/LGD/Expected Loss/RWA. rates = repricing gap and HHI.",
    )
    parser.add_argument(
        "--source-uri", default=os.environ.get("SOURCE_URI"),
        help="s3:// URI of the CSV riskforge-execute-sql wrote.",
    )
    parser.add_argument(
        "--output-uri", default=os.environ.get("OUTPUT_URI"),
        help="s3:// URI to write this branch's aggregates to.",
    )
    # Endpoint names come from the task definition's environment, set by
    # Terraform from the aws_sagemaker_endpoint resources -- so they are never
    # typed twice, and a rename in infra/sagemaker.tf reaches the container
    # without an image rebuild.
    parser.add_argument("--pd-endpoint", default=os.environ.get("PD_ENDPOINT"))
    parser.add_argument("--lgd-endpoint", default=os.environ.get("LGD_ENDPOINT"))
    parser.add_argument(
        "--batch-rows", type=int, default=int(os.environ.get("BATCH_ROWS") or 0),
        help="Rows per endpoint request. 0 uses the default in scoring.py.",
    )
    parser.add_argument(
        "--workers", type=int, default=int(os.environ.get("WORKERS") or 0),
        help="Concurrent endpoint requests. 0 uses the endpoint's max_concurrency.",
    )
    # Where PD and LGD are computed. "local" runs the endpoints' own artifacts
    # through the endpoints' own handler in this process; "endpoint" sends the
    # portfolio to SageMaker. Default local because that is what the deployed
    # pipeline uses -- 880 requests at five concurrent was most of the latency --
    # and "endpoint" is kept because it is the fallback that needs no rebuild.
    parser.add_argument(
        "--scoring", choices=SCORING_MODES,
        default=os.environ.get("SCORING_MODE", "local"),
        help="local = boosters in this process. endpoint = SageMaker Runtime.",
    )

    args = parser.parse_args(argv)

    problems = []
    if args.mode not in MODES:
        problems.append("--mode must be one of %s (or RISKFORGE_MODE)" % (MODES,))
    if not args.source_uri:
        problems.append("--source-uri is required (or SOURCE_URI)")
    if not args.output_uri:
        problems.append("--output-uri is required (or OUTPUT_URI)")
    if args.scoring not in SCORING_MODES:
        problems.append("--scoring must be one of %s (or SCORING_MODE)" % (SCORING_MODES,))
    # Required in endpoint mode only. In local mode the names are recorded as
    # provenance rather than called, so a missing one is not a reason to refuse to
    # score -- but they are passed anyway by both Step Functions branches.
    if args.mode == "score" and args.scoring == "endpoint" \
            and not (args.pd_endpoint and args.lgd_endpoint):
        problems.append("--pd-endpoint and --lgd-endpoint are required in score mode "
                        "with --scoring endpoint (or PD_ENDPOINT / LGD_ENDPOINT)")
    if problems:
        parser.error("; ".join(problems))

    return args


def compute(args):
    """
    The work, with no process or transport in it: read, branch, write, return the
    URI. Shared by the CLI entrypoint and the Lambda handler so that the two
    cannot drift -- everything above this line is argument handling and everything
    below is how the caller reports failure.
    """
    started = dt.datetime.now(dt.timezone.utc)
    out_bucket, out_key = inputs.parse_uri(args.output_uri)

    raw_df = inputs.load_query_result(args.source_uri)

    if args.mode == "score":
        payload = credit.run(
            raw_df, args.pd_endpoint, args.lgd_endpoint,
            batch_rows=args.batch_rows or None, workers=args.workers or None,
            scoring=args.scoring,
        )
    else:
        payload = rates.run(raw_df)

    envelope = outputs.envelope(
        args.mode, args.source_uri, payload, started, len(raw_df))
    return outputs.write(envelope, out_bucket, out_key), started, len(raw_df)


def write_failure(args, exc, started):
    """
    The failure, written to the output key as well as raised.

    The reason has to be somewhere the state machine can read: a Fargate task
    returns only an exit code, and a Lambda's error payload is truncated and
    carries no context about which query it was computing. CloudWatch Logs is not
    that place either, because a state machine cannot read it. So the object at the
    output key is either a result or an explanation, and never absent.
    """
    out_bucket, out_key = inputs.parse_uri(args.output_uri)
    failure = {
        "mode": args.mode,
        "success": False,
        "source_uri": args.source_uri,
        "error": "%s: %s" % (type(exc).__name__, exc),
        "started_at": started.isoformat(),
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    try:
        outputs.write(failure, out_bucket, out_key)
    except Exception as write_failure_exc:
        # Best effort by definition: if S3 is the thing that is broken, the raised
        # exception is all there is, and saying so beats a traceback about the
        # error handler.
        logger.error("task | could not write the failure result | %s" % write_failure_exc)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    started = dt.datetime.now(dt.timezone.utc)

    logger.info("task | mode=%s scoring=%s source=%s output=%s"
                % (args.mode, args.scoring, args.source_uri, args.output_uri))

    try:
        uri, _, _ = compute(args)
    except Exception as exc:
        # Step Functions sees a non-zero exit and can Catch on it, but the exit
        # code says only that the task failed.
        logger.error("task | %s | %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        write_failure(args, exc, started)
        return 1

    print(uri)
    return 0


def handler(event, context):
    """
    The same work as a container-image Lambda, which is how both Step Functions
    branches now run it.

    Why not Fargate any more: a task spends 21-49 seconds attaching an ENI and
    pulling a 150 MB image before any of this file executes, and `ecs:runTask.sync`
    then waits through a further 24-29 seconds of teardown after the result is
    already in S3. Measured across five task lifecycles, the rates branch ran for
    3.2 seconds inside a state that took 52.8. Here there is no ENI at all -- these
    two functions are not VPC-attached, because they reach only S3 and SageMaker
    Runtime and both are public AWS API endpoints (see infra/lambda_risk.tf) -- the
    image layers are cached rather than pulled per request, and a warm environment
    keeps the two loaded boosters between questions.

    **The S3 contract is unchanged, deliberately.** This writes the same object, at
    the same key, in the same envelope shape, so ReadCreditResult, ReadRatesResult
    and everything downstream of them did not have to change -- the state machine
    diff is the compute resource and nothing else. What comes back here is only the
    URI and the row count, well inside the 256 KB state limit, and no per-loan
    field can appear in it because outputs.check refuses to write one in the first
    place.

    Raising rather than returning an error: Step Functions distinguishes a failed
    Lambda from a successful one by the raise, so a returned {"success": false}
    would let a broken branch look like a completed one and carry an empty result
    into Compliance. The explanation still reaches S3, at the output key, because
    write_failure puts it there before this re-raises.
    """
    event = event or {}
    argv = []
    for flag, key in (
        ("--mode", "mode"),
        ("--source-uri", "source_uri"),
        ("--output-uri", "output_uri"),
        ("--pd-endpoint", "pd_endpoint"),
        ("--lgd-endpoint", "lgd_endpoint"),
        ("--scoring", "scoring"),
        ("--batch-rows", "batch_rows"),
        ("--workers", "workers"),
    ):
        value = event.get(key)
        if value not in (None, ""):
            argv.extend([flag, str(value)])

    # Through the same parser as the CLI, so the event is validated by the same
    # rules and a missing mode is still an error rather than a default. argparse
    # calls sys.exit on a bad argument, which inside a Lambda is a SystemExit --
    # caught below and turned into a raise with the usage message attached, because
    # a bare SystemExit in the logs says nothing about what was wrong.
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        raise ValueError(
            "invalid task event: %s. Expected mode, source_uri and output_uri; "
            "got keys %s." % (exc, sorted(event.keys()))
        ) from exc

    started = dt.datetime.now(dt.timezone.utc)
    logger.info("task | lambda | mode=%s scoring=%s source=%s output=%s"
                % (args.mode, args.scoring, args.source_uri, args.output_uri))

    try:
        uri, _, row_count = compute(args)
    except Exception as exc:
        logger.error("task | %s | %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        write_failure(args, exc, started)
        raise

    return {
        "mode": args.mode,
        "success": True,
        "result_uri": uri,
        "row_count": row_count,
        "elapsed_ms": int(
            (dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000),
    }


if __name__ == "__main__":
    sys.exit(main())
