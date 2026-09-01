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

    args = parser.parse_args(argv)

    problems = []
    if args.mode not in MODES:
        problems.append("--mode must be one of %s (or RISKFORGE_MODE)" % (MODES,))
    if not args.source_uri:
        problems.append("--source-uri is required (or SOURCE_URI)")
    if not args.output_uri:
        problems.append("--output-uri is required (or OUTPUT_URI)")
    if args.mode == "score" and not (args.pd_endpoint and args.lgd_endpoint):
        problems.append("--pd-endpoint and --lgd-endpoint are required in score mode "
                        "(or PD_ENDPOINT / LGD_ENDPOINT)")
    if problems:
        parser.error("; ".join(problems))

    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    started = dt.datetime.now(dt.timezone.utc)
    out_bucket, out_key = inputs.parse_uri(args.output_uri)

    logger.info("task | mode=%s source=%s output=%s"
                % (args.mode, args.source_uri, args.output_uri))

    try:
        raw_df = inputs.load_query_result(args.source_uri)

        if args.mode == "score":
            payload = credit.run(
                raw_df, args.pd_endpoint, args.lgd_endpoint,
                batch_rows=args.batch_rows or None, workers=args.workers or None,
            )
        else:
            payload = rates.run(raw_df)

        envelope = outputs.envelope(
            args.mode, args.source_uri, payload, started, len(raw_df))
        uri = outputs.write(envelope, out_bucket, out_key)

    except Exception as exc:
        # The failure is written to the output key as well as logged. Step
        # Functions sees a non-zero exit and can Catch on it, but the exit code
        # says only that the task failed -- the reason has to be somewhere the
        # state machine can read, and CloudWatch Logs is not that place.
        logger.error("task | %s | %s" % (type(exc).__name__, exc))
        traceback.print_exc()
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
        except Exception as write_failure:
            # Best effort by definition: if S3 is the thing that is broken, the
            # exit code is all there is, and saying so beats a traceback about
            # the error handler.
            logger.error("task | could not write the failure result | %s" % write_failure)
        return 1

    print(uri)
    return 0


if __name__ == "__main__":
    sys.exit(main())
