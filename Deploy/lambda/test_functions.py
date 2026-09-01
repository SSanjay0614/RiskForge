"""
Exercises the two deployed VPC Lambdas, including the parts that are supposed to
fail.

    python Deploy/lambda/test_functions.py --profile riskforge

A happy-path smoke test would pass against a function with no read-only
enforcement at all, so most of what follows is adversarial: a write that gets
past the keyword filter on purpose, a stacked statement, a server-side file read.
Each one asserts the *server* refused it, which is the only claim worth making --
the regex in the handler is a source of better error messages, not a boundary.

The compliance check is verified differently: its flags are compared against what
agents/compliance_agent.py's own METRIC_LOOKUP would conclude from the same
inputs, read from the local SQLite Risk_Limits table. That catches the failure
this port could actually have -- a threshold direction flipped, so a breach reads
as a pass.
"""
import argparse
import json
import os
import sqlite3
import sys

import boto3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SQLITE_DB = os.path.join(REPO_ROOT, "Database", "credit_risk.db")

# Metrics chosen to breach some limits and clear others, so a report that says
# "no breaches" is visibly wrong rather than plausibly quiet.
SAMPLE_METRICS = {
    "credit_metrics": {
        "expected_loss_rate": 0.0731,
        "exposure_weighted_avg_pd": 0.1142,
        "exposure_weighted_avg_lgd": 0.4210,
    },
    "rate_metrics": {
        "concentration_by_purpose": {"hhi_score_10000_scale": 4820.0},
        "concentration_by_region": {"hhi_score_10000_scale": 610.0},
        "repricing_gap": {"loan_to_deposit_ratio": 0.8800},
    },
}

DIRECTIONS = {
    "max_expected_loss_rate": ("max", ("credit_metrics", "expected_loss_rate")),
    "max_hhi_10000_scale": ("max", None),
    "pd_floor_retail_other": ("min", ("credit_metrics", "exposure_weighted_avg_pd")),
    "lgd_floor_retail_unsecured_other": ("min", ("credit_metrics", "exposure_weighted_avg_lgd")),
    "max_loan_to_deposit_ratio": ("max", None),
}

results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


def invoke(client, function_name, payload):
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if "FunctionError" in response:
        raise SystemExit(
            "%s raised instead of returning an error:\n%s"
            % (function_name, json.dumps(body, indent=2)[:2000])
        )
    return body


def server_refusal(out, *needles):
    """True only if the server refused for one of the stated reasons.

    A check that accepts *any* failure is worse than no check: with the database
    unreachable, or a broken statement in connect(), every adversarial assertion
    in this file would pass while proving nothing. So a dropped connection and an
    unexpected error are both failures here, and the reason has to match.
    """
    if out.get("success"):
        return False, "succeeded -- it should not have"
    error = out.get("error") or ""
    if "InterfaceError" in error or "network error" in error:
        return False, "connection died, so nothing was proven: " + error[:90]
    if any(n.lower() in error.lower() for n in needles):
        return True, error[:150]
    return False, "refused, but not for the expected reason: " + error[:110]


def test_execute_sql(client, function_name):
    print("execute-sql")

    out = invoke(client, function_name, {"sql_query": "SELECT count(*) AS n FROM loans"})
    record(
        "aggregate returns inline",
        out.get("success") and out.get("rows") and out["rows"][0][0] == 878317,
        json.dumps(out.get("rows")),
    )
    record("result written to s3", bool(out.get("s3_key")), out.get("s3_key", ""))

    out = invoke(client, function_name, {"sql_query": "DELETE FROM loans WHERE loan_id = 1"})
    record(
        "non-SELECT refused by the pre-filter",
        not out.get("success") and "SELECT" in (out.get("error") or ""),
        out.get("error", ""),
    )

    # Passes the SELECT-only check and the keyword filter -- `INTO` is not a
    # blocked keyword and the statement does start with SELECT -- so the only
    # thing that can stop it is the server. If this one succeeds, read-only is
    # not real.
    out = invoke(
        client, function_name, {"sql_query": "SELECT loan_id INTO leaked FROM loans LIMIT 1"}
    )
    record(
        "SELECT INTO refused by the server, not the regex",
        *server_refusal(out, "read-only", "permission denied")
    )

    out = invoke(
        client, function_name, {"sql_query": "SELECT 1; DROP TABLE loans"}
    )
    record(
        "stacked statement refused",
        not out.get("success"),
        (out.get("error") or "")[:120],
    )

    # A semicolon inside a string literal is legitimate and must not be mistaken
    # for a second statement.
    out = invoke(
        client, function_name, {"sql_query": "SELECT ';' AS semicolon_in_a_literal"}
    )
    record("semicolon inside a literal allowed", bool(out.get("success")), out.get("error", ""))

    out = invoke(client, function_name, {"sql_query": "SELECT pg_read_file('/etc/passwd')"})
    record(
        "server-side file read refused",
        *server_refusal(out, "permission denied", "does not exist")
    )

    out = invoke(client, function_name, {"sql_query": "SELECT * FROM pg_shadow"})
    record(
        "password catalog unreadable",
        *server_refusal(out, "permission denied", "does not exist")
    )

    out = invoke(
        client,
        function_name,
        {"sql_query": "SELECT loan_id FROM loans", "max_rows": 5000},
    )
    record(
        "row cap truncates and says so",
        out.get("success") and out.get("truncated") and out.get("row_count") == 5000,
        "row_count=%s truncated=%s" % (out.get("row_count"), out.get("truncated")),
    )
    record(
        "bulk rows withheld from the response",
        out.get("rows") is None and bool(out.get("rows_withheld_reason")),
        (out.get("rows_withheld_reason") or "")[:100],
    )

def local_expectation():
    """What the limits in the local SQLite table imply for SAMPLE_METRICS, worked
    out independently of the Lambda so the two can disagree."""
    if not os.path.exists(SQLITE_DB):
        return None

    connection = sqlite3.connect("file:%s?mode=ro" % SQLITE_DB, uri=True)
    try:
        rows = connection.execute(
            "SELECT metric_name, threshold FROM Risk_Limits"
        ).fetchall()
    finally:
        connection.close()

    hhi = max(
        SAMPLE_METRICS["rate_metrics"]["concentration_by_purpose"]["hhi_score_10000_scale"],
        SAMPLE_METRICS["rate_metrics"]["concentration_by_region"]["hhi_score_10000_scale"],
    )
    ltd = SAMPLE_METRICS["rate_metrics"]["repricing_gap"]["loan_to_deposit_ratio"]

    expected = {}
    for metric_name, threshold in rows:
        direction, path = DIRECTIONS.get(metric_name, (None, None))
        if direction is None:
            continue
        if metric_name == "max_hhi_10000_scale":
            value = hhi
        elif metric_name == "max_loan_to_deposit_ratio":
            value = ltd
        else:
            value = SAMPLE_METRICS[path[0]][path[1]]
        expected[metric_name] = (value > threshold) if direction == "max" else (value < threshold)
    return expected


def test_compliance_check(client, function_name):
    print("compliance-check")

    out = invoke(client, function_name, SAMPLE_METRICS)
    if not out.get("success"):
        record("invocation", False, out.get("error", ""))
        return

    flags = {f["metric_name"]: f for f in out["flags"]}
    record(
        "all five limits checked",
        out.get("limits_checked") == 5 and not out.get("skipped"),
        "checked=%s in_table=%s skipped=%s"
        % (out.get("limits_checked"), out.get("limits_in_table"), out.get("skipped")),
    )

    expected = local_expectation()
    if expected is None:
        record("matches the local ComplianceAgent", False, "local SQLite database not present")
    else:
        disagreements = [
            "%s: lambda=%s local=%s" % (name, flags.get(name, {}).get("breached"), breached)
            for name, breached in expected.items()
            if flags.get(name, {}).get("breached") != breached
        ]
        record(
            "matches the local ComplianceAgent on every limit",
            not disagreements,
            "; ".join(disagreements),
        )

    # A breach must arrive with its citation attached; a flag without one is a
    # finding an analyst cannot act on.
    uncited = [name for name, f in flags.items() if f["breached"] and not f.get("citation")]
    record("every breach carries a citation", not uncited, ", ".join(uncited))

    basel = [f for f in flags.values() if f["source"] == "basel_iii"]
    record(
        "Basel limits are floors, not ceilings",
        basel and all(f["direction"] == "min" for f in basel),
        "directions=%s" % sorted({f["direction"] for f in basel}),
    )

    record(
        "regulatory capital deliberately absent",
        out.get("regulatory_capital") is None and bool(out.get("regulatory_capital_note")),
    )

    for name in sorted(flags):
        f = flags[name]
        print(
            "       %-34s value=%-10.4f threshold=%-10.4f %-3s %s"
            % (name, f["value"], f["threshold"], f["direction"],
               "BREACH" if f["breached"] else "ok")
        )

    # SAMPLE_METRICS breaches only internal limits, so the branch that cites
    # risk_limits.description rather than BREACH_NOTES goes untested unless a
    # Basel floor is breached too. PD and LGD below their floors do that.
    below_floors = json.loads(json.dumps(SAMPLE_METRICS))
    below_floors["credit_metrics"]["exposure_weighted_avg_pd"] = 0.0001
    below_floors["credit_metrics"]["exposure_weighted_avg_lgd"] = 0.10

    out = invoke(client, function_name, below_floors)
    basel_flags = [f for f in out.get("flags", []) if f["source"] == "basel_iii"]
    record(
        "Basel floors breach downward",
        len(basel_flags) == 2 and all(f["breached"] for f in basel_flags),
        "breached=%s" % [f["breached"] for f in basel_flags],
    )
    record(
        "Basel breaches cite the table's own source paragraph",
        all(f.get("citation") and "CRE" in f["citation"] for f in basel_flags),
        (basel_flags[0].get("citation") or "")[:110] if basel_flags else "no basel flags",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--execute-sql", default="riskforge-execute-sql")
    parser.add_argument("--compliance-check", default="riskforge-compliance-check")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("lambda")

    test_execute_sql(client, args.execute_sql)
    test_compliance_check(client, args.compliance_check)

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print("%d of %d checks failed: %s" % (len(failed), len(results), ", ".join(failed)))
        sys.exit(1)
    print("all %d checks passed" % len(results))


if __name__ == "__main__":
    main()
