"""
Starts one execution of riskforge-pipeline and follows it.

    python Deploy/stepfunctions/run_pipeline.py --profile riskforge \
        "How risky is our California 60-month loan book issued since 2018?"

One question in, one JSON answer out, and the states printed as they are entered
so the fan-out is visible while it happens rather than reconstructed afterwards.

Nothing is created here. The state machine, its role and the two Fargate branches
are Terraform's (infra/stepfunctions.tf, infra/iam_sfn.tf). This script starts an
execution, polls it, and prints what the pipeline decided -- which is also the
shape Phase 12's Streamlit app consumes, so the printout is deliberately the
fields that belong on a page and nothing else.

Note what this script never prints: a loan. The execution output cannot carry one
-- riskforge/outputs.py refuses to write a per-loan field and the BuildProfile
state drops any row execute-sql returned inline -- and this reads named fields
rather than dumping whatever came back, so a future field cannot leak through it
either.
"""
import argparse
import json
import sys
import time
import uuid

import boto3

PROJECT = "riskforge"
MACHINE = "%s-pipeline" % PROJECT
TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED")

# Which history events are worth a line. StateEntered is the path through the
# graph; the two failure events are why it stopped there.
INTERESTING = {
    "TaskStateEntered", "ChoiceStateEntered", "PassStateEntered",
    "ParallelStateEntered", "SucceedStateEntered", "FailStateEntered",
    "ExecutionFailed", "TaskFailed", "ExecutionAborted", "ExecutionTimedOut",
}


def find_machine(sfn):
    """By name, not by a copy of the ARN. `terraform output
    pipeline_state_machine_arn` is the other way to get it."""
    paginator = sfn.get_paginator("list_state_machines")
    for page in paginator.paginate():
        for machine in page["stateMachines"]:
            if machine["name"] == MACHINE:
                return machine["stateMachineArn"]
    raise SystemExit(
        "%s does not exist. Apply infra/stepfunctions.tf first." % MACHINE)


def execution_name(prefix="run"):
    """The name is not decoration: PlanRun formats both S3 keys from
    $$.Execution.Name, so this string becomes a key segment under
    risk-results/. Hex and a dash only -- no slash, no space, and unique per
    run so two executions never write over each other's aggregates."""
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


def describe_event(event):
    """One history event, one line. The state name lives in a details field
    whose key depends on the event type, hence the two lookups."""
    kind = event["type"]
    details = event.get("stateEnteredEventDetails") or {}
    name = details.get("name")
    if name:
        return "%-22s %s" % (kind.replace("StateEntered", ""), name)

    for key in ("executionFailedEventDetails", "taskFailedEventDetails",
                "executionAbortedEventDetails", "executionTimedOutEventDetails"):
        failure = event.get(key)
        if failure:
            return "%-22s %s: %s" % (
                kind, failure.get("error", "-"),
                (failure.get("cause") or "")[:400])
    return kind


def follow(sfn, arn, timeout, quiet):
    """Poll the history and the status together, printing each new event once.
    Sequential ids are monotonic, so the last one seen is the whole cursor."""
    seen = 0
    deadline = time.time() + timeout
    while True:
        if not quiet:
            history = sfn.get_execution_history(
                executionArn=arn, maxResults=1000, includeExecutionData=False)
            for event in history["events"]:
                if event["id"] > seen and event["type"] in INTERESTING:
                    print("  %s" % describe_event(event))
                seen = max(seen, event["id"])

        status = sfn.describe_execution(executionArn=arn)
        if status["status"] in TERMINAL:
            return status
        if time.time() > deadline:
            raise SystemExit(
                "\nStill RUNNING after %ss. The execution is not cancelled -- "
                "follow it in the console, or stop it with\n  aws stepfunctions "
                "stop-execution --execution-arn %s" % (timeout, arn))
        time.sleep(2)


def money(value):
    return "-" if value is None else "$%s" % format(float(value), ",.2f")


def pct(value, scale=100.0):
    return "-" if value is None else "%.2f%%" % (float(value) * scale)


def print_refusal(data):
    print("\nRefused by the %s." % data.get("rejected_by", "guard"))
    print("  question : %s" % data.get("query"))
    print("  reason   : %s" % data.get("reason"))


def print_population(data):
    print("\nPopulation")
    print("  question   : %s" % data.get("query"))
    print("  rows       : %s" % data.get("row_count"))
    print("  retries    : %s" % data.get("retries"))
    print("  source     : %s" % data.get("source_uri"))
    print("  sql        : %s" % " ".join(str(data.get("sql_query", "")).split()))


def print_credit(metrics, capital, elapsed):
    print("\nCredit risk    (%s ms)" % elapsed)
    print("  loans scored : %s of %s retrieved (%s dropped)" % (
        metrics.get("loan_count"), metrics.get("rows_retrieved"),
        metrics.get("rows_dropped")))
    print("  exposure     : %s" % money(metrics.get("total_exposure")))
    print("  expected loss: %s  (%s of exposure)" % (
        money(metrics.get("total_expected_loss")),
        pct(metrics.get("expected_loss_rate"))))
    print("  weighted PD  : %s" % pct(metrics.get("exposure_weighted_avg_pd")))
    print("  weighted LGD : %s" % pct(metrics.get("exposure_weighted_avg_lgd")))
    tiers = metrics.get("risk_tier_distribution") or {}
    if tiers:
        print("  tier mix     : %s" % ", ".join(
            "%s %s" % (tier, pct(share)) for tier, share in tiers.items()))
    if capital:
        print("  RWA          : %s  (risk weight %.2f%%)" % (
            money(capital.get("total_rwa")),
            float(capital.get("avg_risk_weight_pct") or 0.0)))
        print("  capital @8%%  : %s" % money(
            capital.get("total_capital_requirement_8pct")))


def print_rates(metrics, elapsed):
    print("\nInterest rate risk    (%s ms)" % elapsed)
    gap = metrics.get("repricing_gap")
    if not gap:
        print("  repricing gap: skipped")
    else:
        print("  as of        : %s" % gap.get("as_of_date"))
        print("  net gap      : %s (%s sensitive)" % (
            money(gap.get("net_gap")),
            "liability" if gap.get("is_liability_sensitive") else "asset"))
        print("  NII / NIM    : %s / %s" % (
            money(gap.get("net_interest_income_annual")),
            pct(gap.get("net_interest_margin"))))
        print("  EaR 12m      : %s" % money(gap.get("earnings_at_risk_12m")))

    for label, key in (("purpose", "concentration_by_purpose"),
                       ("region", "concentration_by_region")):
        hhi = metrics.get(key)
        if not hhi:
            print("  HHI %-9s: skipped" % label)
        else:
            print("  HHI %-9s: %.1f (%s)" % (
                label, float(hhi.get("hhi_score_10000_scale") or 0.0),
                hhi.get("diversification_level")))


def print_compliance(check):
    if not check:
        return
    breaches = [f for f in check.get("flags", []) if f.get("breached")]
    print("\nCompliance    %s of %s limits checked, %s" % (
        check.get("limits_checked"), check.get("limits_in_table"),
        "%d BREACHED" % len(breaches) if breaches else "all within limit"))
    for flag in breaches:
        print("  ! %-28s %s vs %s %s" % (
            flag.get("metric_name"), flag.get("value"),
            flag.get("direction"), flag.get("threshold")))
        if flag.get("citation"):
            print("      %s" % flag["citation"])
    for entry in (check.get("skipped") or []):
        print("  - %-28s skipped: %s" % (
            entry.get("metric_name"), entry.get("reason")))
    if check.get("regulatory_capital_note"):
        print("  note: %s" % check["regulatory_capital_note"])


def report(output):
    """The execution output, read field by field. Three shapes reach here: a
    refusal, a query the guard said needs no risk analysis, and the full
    fan-out. All three are SUCCEEDED executions -- a refusal is an answer."""
    data = json.loads(output)
    if not data.get("answered"):
        print_refusal(data)
        return
    print_population(data)
    if not data.get("risk_analysis"):
        print("\nThe guard judged this a data question, so neither risk branch ran.")
        return
    print_credit(data.get("credit_metrics") or {},
                 data.get("regulatory_capital") or {},
                 data.get("credit_elapsed_ms"))
    print_rates(data.get("rate_metrics") or {}, data.get("rates_elapsed_ms"))
    print_compliance(data.get("compliance") or {})
    print("\nAggregates: %s\n            %s" % (
        data.get("credit_uri"), data.get("rates_uri")))


def main():
    parser = argparse.ArgumentParser(
        description="Run one question through riskforge-pipeline.")
    parser.add_argument("question", help="The analyst's question, in English.")
    parser.add_argument("--profile", default=PROJECT)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--name", help="Execution name. Generated if omitted.")
    parser.add_argument("--timeout", type=int, default=1200,
                        help="Seconds to follow before giving up on polling. "
                             "The execution's own timeout is set in Terraform.")
    parser.add_argument("--quiet", action="store_true",
                        help="Skip the state-by-state trace.")
    parser.add_argument("--json", action="store_true",
                        help="Print the raw execution output instead of a report.")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    sfn = session.client("stepfunctions")
    arn = find_machine(sfn)
    name = args.name or execution_name()

    print("%s  ->  %s" % (MACHINE, name))
    started = time.time()
    execution = sfn.start_execution(
        stateMachineArn=arn, name=name,
        input=json.dumps({"query": args.question}))

    status = follow(sfn, execution["executionArn"], args.timeout, args.quiet)
    print("\n%s in %.1fs" % (status["status"], time.time() - started))

    if status["status"] != "SUCCEEDED":
        print("  error : %s" % status.get("error"))
        print("  cause : %s" % (status.get("cause") or "")[:1000])
        print("\nFull history:\n  aws stepfunctions get-execution-history "
              "--profile %s --execution-arn %s"
              % (args.profile, execution["executionArn"]))
        return 1

    if args.json:
        print(status["output"])
    else:
        report(status["output"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
