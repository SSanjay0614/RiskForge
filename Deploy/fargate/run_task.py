"""
Starts both risk branches as two concurrent Fargate tasks and waits for them.

    python Deploy/fargate/run_task.py --profile riskforge
    python Deploy/fargate/run_task.py --profile riskforge --source-uri s3://...

This is the fan-out the Step Functions Parallel state performs in Phase 11, done
here with two `ecs run_task` calls. It exists for two reasons and neither is
"instead of the state machine":

  * It is how the built image gets tested. Until a state machine exists there is
    no other way to run the container the way it will actually be run -- task
    role, no shell, environment from the task definition, command as an override.
  * It shows what the state machine is doing. Two tasks start within a second of
    each other, run on separate infrastructure, and write to two keys under one
    run prefix; the wall-clock total is the slower of the two, not the sum. That
    is the claim the Parallel state makes, and this is the version of it you can
    watch happen.

Both tasks read the same query result and neither reads the other's output, so
there is nothing to serialise. The scoring branch takes about 40 seconds and the
rates branch about 5, so running them in sequence would cost the 5.

Nothing is created here: the cluster, task definition, roles and security group
are Terraform's (infra/ecs.tf, infra/iam_ecs.tf). This script only starts tasks.
"""
import argparse
import json
import os
import sys
import uuid

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))

PROJECT = "riskforge"
CONTAINER = "risk"

# Mirrors data.aws_subnets.db in infra/data.tf. Pinned to the same three AZs for
# the same reason: us-east-1e has historically lacked capacity, and a run_task
# that lands there fails with a capacity error that has nothing to do with this
# code.
AZS = ["us-east-1a", "us-east-1b", "us-east-1c"]


def discover(session):
    """Cluster, task definition, subnets, security group and bucket, from AWS
    rather than from a copy of the Terraform values kept in step by hand."""
    account = session.client("sts").get_caller_identity()["Account"]
    ec2 = session.client("ec2")

    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc]},
        {"Name": "availability-zone", "Values": AZS},
    ])["Subnets"]]
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": ["%s-task-sg" % PROJECT]},
        {"Name": "vpc-id", "Values": [vpc]},
    ])["SecurityGroups"]
    if not groups:
        raise SystemExit(
            "%s-task-sg does not exist. Apply infra/ecs.tf and infra/iam_ecs.tf "
            "first." % PROJECT)

    return {
        "cluster": "%s-cluster" % PROJECT,
        "task_definition": "%s-risk" % PROJECT,
        "subnets": subnets,
        "security_groups": [groups[0]["GroupId"]],
        "bucket": "%s-artifacts-%s" % (PROJECT, account),
    }


def query(session, sql, max_rows):
    """One call to riskforge-execute-sql. max_inline_rows is 1 because this
    script wants the key and the row count and never a row."""
    print("querying riskforge-execute-sql")
    response = session.client("lambda").invoke(
        FunctionName="%s-execute-sql" % PROJECT,
        Payload=json.dumps({"sql_query": sql, "max_rows": max_rows,
                            "max_inline_rows": 1}).encode("utf-8"),
    )
    payload = json.loads(response["Payload"].read().decode("utf-8"))
    if not payload.get("success"):
        raise SystemExit("query failed: %s" % payload.get("error"))
    print("  %d rows, %d columns, %s"
          % (payload["row_count"], len(payload["columns"]), payload["s3_uri"]))
    return payload["s3_uri"]


def start(ecs, config, mode, source_uri, output_uri):
    task = ecs.run_task(
        cluster=config["cluster"],
        taskDefinition=config["task_definition"],
        launchType="FARGATE",
        count=1,
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": config["subnets"],
            "securityGroups": config["security_groups"],
            # Required for a task in a public subnet to reach ECR at all: without
            # a public IP there is no route to the registry, and the task fails
            # before the container starts with a pull timeout.
            "assignPublicIp": "ENABLED",
        }},
        overrides={"containerOverrides": [{
            "name": CONTAINER,
            # The task definition carries no command, so this is the only place
            # the mode is decided -- which is what makes one image two agents.
            "command": ["--mode", mode,
                        "--source-uri", source_uri,
                        "--output-uri", output_uri],
        }]},
    )
    failures = task.get("failures") or []
    if failures:
        raise SystemExit("run_task failed: %s" % failures)
    arn = task["tasks"][0]["taskArn"]
    print("  %-6s %s" % (mode, arn.rsplit("/", 1)[-1]))
    return arn

def wait(ecs, config, arns):
    """
    Both tasks, waited on together. `tasks_stopped` polls every 6 seconds for up
    to 100 attempts, which is 10 minutes -- longer than either branch needs and
    short enough that a task stuck in PROVISIONING does not hang this forever.
    """
    print("waiting for both tasks to stop")
    ecs.get_waiter("tasks_stopped").wait(cluster=config["cluster"], tasks=arns)
    described = ecs.describe_tasks(cluster=config["cluster"], tasks=arns)["tasks"]

    results = {}
    for task in described:
        container = task["containers"][0]
        results[task["taskArn"]] = {
            "exit_code": container.get("exitCode"),
            "reason": container.get("reason") or task.get("stoppedReason"),
            # The log stream, printed whether the task passed or failed: a task
            # that returns a non-zero exit code says nothing about why, and this
            # is where the traceback is.
            "log_stream": "task/%s/%s" % (CONTAINER, task["taskArn"].rsplit("/", 1)[-1]),
        }
    return results

def summarise(s3, uri):
    """The envelope's header, not the numbers. test_task.py compares the numbers
    against the local models; this only says the object is there and well formed."""
    bucket, key = uri[len("s3://"):].split("/", 1)
    doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
    return "%s | success=%s | %d rows | %d ms" % (
        doc.get("mode"), doc.get("success"), doc.get("row_count") or 0,
        doc.get("elapsed_ms") or 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--source-uri", help="a query-result CSV; queried if omitted")
    parser.add_argument("--sql", default=(
        "SELECT * FROM loans JOIN borrowers USING(loan_id) "
        "WHERE addr_state = 'CA' AND term = 60 AND issue_date >= '2018-01-01'"))
    parser.add_argument("--max-rows", type=int, default=200000)
    parser.add_argument("--run-id", help="output prefix; a uuid if omitted")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    config = discover(session)
    ecs = session.client("ecs")
    s3 = session.client("s3")

    source_uri = args.source_uri or query(session, args.sql, args.max_rows)
    run_id = args.run_id or uuid.uuid4().hex[:12]
    outputs = {
        mode: "s3://%s/risk-results/%s/%s.json" % (
            config["bucket"], run_id, "credit" if mode == "score" else "rates")
        for mode in ("score", "rates")
    }

    print("starting two tasks in %s" % config["cluster"])
    arns = {mode: start(ecs, config, mode, source_uri, outputs[mode])
            for mode in ("score", "rates")}

    results = wait(ecs, config, list(arns.values()))

    print()
    failed = False
    for mode, arn in arns.items():
        result = results[arn]
        state = "ok" if result["exit_code"] == 0 else "FAILED"
        print("%-6s %-6s exit=%s  log=%s"
              % (mode, state, result["exit_code"], result["log_stream"]))
        if result["exit_code"] != 0:
            failed = True
            print("       reason: %s" % result["reason"])
        else:
            print("       %s" % summarise(s3, outputs[mode]))

    print()
    print("source     %s" % source_uri)
    print("credit     %s" % outputs["score"])
    print("rates      %s" % outputs["rates"])
    if failed:
        print()
        print("logs: aws logs tail /ecs/%s-risk --profile %s" % (PROJECT, args.profile))
        return 1

    print()
    print("Compare the container's numbers against the local models:")
    print("  python %s --profile %s --source-uri %s --credit-uri %s --rates-uri %s"
          % (os.path.join(HERE, "test_task.py"), args.profile or "default",
             source_uri, outputs["score"], outputs["rates"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
