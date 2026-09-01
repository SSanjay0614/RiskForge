"""
Checks the container's numbers against the local pipeline's, on the same rows.

    python Deploy/fargate/test_task.py --profile riskforge
    python Deploy/fargate/test_task.py --profile riskforge
        --source-uri s3://.../query-results/<id>.csv
        --credit-uri s3://.../risk-results/<run>/credit.json
        --rates-uri s3://.../risk-results/<run>/rates.json

**This runs outside the container, and that is what makes it a test.** The
container has no PD or LGD model file in it -- scoring is two SageMaker calls.
This script has both models on disk, loads them with joblib exactly as
tools/expected_loss_tool.py does, and computes Expected Loss, risk-weighted
assets, repricing gap and HHI the way the local application computes them. Then
it compares, field by field, against the JSON the container wrote to S3.

So the claim being tested is the one that matters: that moving PD and LGD onto
endpoints and the tools into a container did not change the answer.
Deploy/sagemaker/verify_endpoints.py already established that the endpoints
reproduce the local models on fixed reference vectors; this establishes that the
whole branch reproduces the local branch on a real query result, including the
feature engineering, the aggregation and the Basel formula on top.

With no --credit-uri and --rates-uri, the two branches are run in-process first
and the comparison is against those -- which tests this repository's code. Given
the two URIs from a real ECS task run, the same comparison tests the built image.
The second is the one to run after build_and_push.py.

Tolerance is 1e-9 relative. Not zero, because the endpoint's numbers arrive as
JSON text and a sum over 16,000 float64 values depends on summation order, which
differs between one local array and forty batched responses. Not looser, because
XGBoost 3.0-5 reading a 2.1.4 model file is supposed to be bit-identical and
verify_endpoints.py confirms it is -- so anything above float noise here is a
finding, not a rounding difference.
"""
import argparse
import datetime as dt
import json
import os
import sys

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from riskforge import credit, inputs, outputs, rates  # noqa: E402

import config  # noqa: E402
from tools.concentration_tool import ConcentrationTool  # noqa: E402
from tools.expected_loss_tool import ExpectedLossTool  # noqa: E402
from tools.feature_engineering_tool import FeatureEngineeringTool  # noqa: E402
from tools.regulatory_capital_tool import RegulatoryCapitalTool  # noqa: E402
from tools.repricing_gap_tool import RepricingGapTool  # noqa: E402

RTOL = 1e-9

# A slice big enough to exercise batching (the default batch is 2,000 rows) and
# small enough to run in under a minute. California, 60-month, 2018 vintage is
# also the adverse corner of this book, which is where a broken feature would
# show up as a plausible number rather than an obvious one.
DEFAULT_SQL = (
    "SELECT * FROM loans JOIN borrowers USING(loan_id) "
    "WHERE addr_state = 'CA' AND term = 60 AND issue_date >= '2018-01-01'"
)


class Checks:
    def __init__(self):
        self.passed = 0
        self.failures = []

    def close(self, label, actual, expected):
        if actual is None or expected is None:
            self.failures.append("%s: got %r, expected %r" % (label, actual, expected))
            return
        scale = max(abs(float(expected)), 1.0)
        if abs(float(actual) - float(expected)) <= RTOL * scale:
            self.passed += 1
        else:
            self.failures.append(
                "%s: %.12g != %.12g (relative %.3g)"
                % (label, actual, expected, abs(float(actual) - float(expected)) / scale)
            )

    def equal(self, label, actual, expected):
        if actual == expected:
            self.passed += 1
        else:
            self.failures.append("%s: %r != %r" % (label, actual, expected))

    def true(self, label, condition, detail=""):
        if condition:
            self.passed += 1
        else:
            self.failures.append("%s%s" % (label, (": " + detail) if detail else ""))

    def report(self):
        print()
        if self.failures:
            print("%d checks passed, %d FAILED" % (self.passed, len(self.failures)))
            for failure in self.failures:
                print("  FAIL  %s" % failure)
            return 1
        print("all %d checks passed" % self.passed)
        return 0


def local_credit(raw_df):
    """
    agents/credit_risk_agent.py, run here, with the two joblib models.

    The exposure_at_default alias is applied where that agent applies it -- after
    engineering, before ExpectedLossTool -- because FeatureEngineeringTool
    validates against the raw schema name and the scoring step needs the exposure
    name. riskforge/features.py does the same thing in the same place.
    """
    fe = FeatureEngineeringTool().run(raw_df)
    engineered = fe.engineered_df.copy()
    if ("exposure_at_default" not in engineered.columns
            and "outstanding_balance" in engineered.columns):
        engineered["exposure_at_default"] = engineered["outstanding_balance"]

    el = ExpectedLossTool().run(engineered)
    capital = RegulatoryCapitalTool().run(el.scored_df)
    return fe, el, capital


def local_rates(raw_df):
    """agents/interest_rate_concentration_agent.py, run here. Raw rows, not
    engineered ones -- HHI by state needs the string CA."""
    tool = ConcentrationTool()
    return (RepricingGapTool().run(raw_df),
            tool.run(raw_df, segment_column="purpose"),
            tool.run(raw_df, segment_column="addr_state"))

def compare_credit(checks, deployed, raw_df):
    """`deployed` is the `result` object of a score-mode run."""
    fe, el, capital = local_credit(raw_df)

    metrics = deployed.get("credit_metrics") or {}
    capital_out = deployed.get("regulatory_capital") or {}

    checks.equal("credit.loan_count", metrics.get("loan_count"), el.loan_count)
    checks.equal("credit.rows_retrieved", metrics.get("rows_retrieved"), fe.input_row_count)
    checks.equal("credit.rows_engineered", metrics.get("rows_engineered"), fe.output_row_count)
    checks.equal("credit.rows_dropped", metrics.get("rows_dropped"), fe.rows_dropped)
    checks.equal("credit.dropped_reason_counts",
                 metrics.get("dropped_reason_counts"), fe.dropped_reason_counts)

    for field in ("total_exposure", "total_expected_loss", "expected_loss_rate",
                  "exposure_weighted_avg_pd", "exposure_weighted_avg_lgd"):
        checks.close("credit." + field, metrics.get(field), getattr(el, field))

    # The tier distribution is the one number the two paths take from different
    # places: the endpoint applies the cutoffs inside its own model artifact, the
    # local tool applies Models/pd_risk_tier_cutoffs.joblib. They are supposed to
    # be the same cutoffs, and this is the check that says so.
    local_tiers = el.risk_tier_distribution
    deployed_tiers = metrics.get("risk_tier_distribution") or {}
    checks.equal("credit.risk_tier keys", sorted(deployed_tiers), sorted(local_tiers))
    for tier, share in local_tiers.items():
        checks.close("credit.risk_tier[%s]" % tier, deployed_tiers.get(tier), share)

    checks.equal("capital.loan_count", capital_out.get("loan_count"), capital.loan_count)
    for field in ("total_ead", "total_rwa", "total_capital_requirement_8pct",
                  "avg_risk_weight_pct", "exposure_weighted_avg_correlation",
                  "exposure_weighted_avg_k"):
        checks.close("capital." + field, capital_out.get(field), getattr(capital, field))

    # The exposure split by tier is the container's own addition, so there is no
    # local number to compare it against. It is checked for internal consistency
    # instead: a split that does not add back up to the totals it was derived
    # from is wrong whatever the local pipeline says.
    by_tier = metrics.get("exposure_by_risk_tier") or {}
    checks.true("credit.exposure_by_risk_tier is populated", bool(by_tier))
    if by_tier:
        checks.close("credit.tier shares sum to 1",
                     sum(v["exposure_share"] for v in by_tier.values()), 1.0)
        checks.close("credit.tier exposure sums to total",
                     sum(v["exposure"] for v in by_tier.values()), el.total_exposure)
        checks.close("credit.tier losses sum to total",
                     sum(v["expected_loss"] for v in by_tier.values()),
                     el.total_expected_loss)
        checks.equal("credit.tier counts sum to scored",
                     sum(v["loan_count"] for v in by_tier.values()), el.loan_count)

    # Coverage is reported rather than asserted at a value, except addr_state:
    # a state that is not in the frequency map is a state the training data never
    # saw, and on a US loan book that means the map is the wrong artifact.
    checks.true("credit.emp_title_coverage is reported",
                metrics.get("emp_title_coverage") is not None,
                "absent from the output")
    checks.equal("credit.addr_state_coverage", metrics.get("addr_state_coverage"), 1.0)

    # Nothing per-loan may appear in the output. outputs.py enforces this before
    # the write; this asserts it on what actually landed in S3, which is the side
    # a reader can check.
    for banned in ("scored_df", "rows", "loan_id", "predicted_pd", "predicted_lgd"):
        checks.true("credit output carries no %s" % banned, banned not in metrics)

def compare_rates(checks, deployed, raw_df):
    """`deployed` is the `result` object of a rates-mode run."""
    gap, purpose, region = local_rates(raw_df)
    metrics = deployed.get("rate_metrics") or {}
    gap_out = metrics.get("repricing_gap") or {}

    checks.true("gap was computed", bool(gap_out),
                "skipped: %r" % (metrics.get("skipped"),))
    checks.equal("gap.as_of_date", gap_out.get("as_of_date"), gap.as_of_date)
    for field in ("total_rate_sensitive_assets", "total_rate_sensitive_liabilities",
                  "net_gap", "portfolio_yield", "deposit_funding_ratio",
                  "deposit_rate_pass_through", "deposit_rate",
                  "interest_income_annual", "interest_expense_annual",
                  "net_interest_income_annual", "net_interest_margin",
                  "loan_to_deposit_ratio", "earnings_at_risk_12m"):
        checks.close("gap." + field, gap_out.get(field), getattr(gap, field))
    checks.equal("gap.is_liability_sensitive",
                 gap_out.get("is_liability_sensitive"), gap.is_liability_sensitive)
    checks.equal("gap.earnings_view_available",
                 gap_out.get("earnings_view_available"), gap.earnings_view_available)
    # The liability side is a documented synthetic assumption. An output that
    # dropped this flag would present it as observed data, so it is asserted
    # rather than assumed to have survived model_dump.
    checks.equal("gap.liabilities_are_synthetic",
                 gap_out.get("liabilities_are_synthetic"), True)

    buckets_out = gap_out.get("buckets") or []
    checks.equal("gap.bucket count", len(buckets_out), len(gap.buckets))
    for actual, expected in zip(buckets_out, gap.buckets):
        checks.equal("gap.bucket label", actual.get("bucket_label"), expected.bucket_label)
        for field in ("rate_sensitive_assets", "rate_sensitive_liabilities",
                      "gap", "cumulative_gap"):
            checks.close("gap[%s].%s" % (expected.bucket_label, field),
                         actual.get(field), getattr(expected, field))

    shocks_out = gap_out.get("rate_shocks") or []
    checks.equal("gap.shock count", len(shocks_out), len(gap.rate_shocks))
    for actual, expected in zip(shocks_out, gap.rate_shocks):
        checks.equal("gap.shock bps", actual.get("shock_bps"), expected.shock_bps)
        for field in ("net_interest_income_change", "net_interest_income_after",
                      "pct_change"):
            checks.close("gap.shock[%+dbp].%s" % (expected.shock_bps, field),
                         actual.get(field), getattr(expected, field))

    for label, expected in (("concentration_by_purpose", purpose),
                            ("concentration_by_region", region)):
        actual = metrics.get(label) or {}
        checks.true("%s was computed" % label, bool(actual))
        checks.equal("%s.segment_column" % label,
                     actual.get("segment_column"), expected.segment_column)
        checks.close("%s.hhi_score" % label, actual.get("hhi_score"), expected.hhi_score)
        checks.close("%s.hhi_score_10000_scale" % label,
                     actual.get("hhi_score_10000_scale"),
                     expected.hhi_score_10000_scale)
        checks.equal("%s.diversification_level" % label,
                     actual.get("diversification_level"),
                     expected.diversification_level)
        shares_out = actual.get("segment_shares") or {}
        checks.equal("%s segment names" % label,
                     sorted(shares_out), sorted(expected.segment_shares))
        for segment, share in expected.segment_shares.items():
            checks.close("%s[%s]" % (label, segment), shares_out.get(segment), share)
        # A share is a share of the population, so the set of them is the
        # population. HHI computed on shares that do not sum to 1 is not HHI.
        if shares_out:
            checks.close("%s shares sum to 1" % label, sum(shares_out.values()), 1.0)

def fetch_json(uri, s3):
    bucket, key = inputs.parse_uri(uri)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body.decode("utf-8"))


def check_envelope(checks, label, doc, mode, source_uri):
    """
    The provenance, not the numbers. A comparison against a result computed from
    some other query would pass or fail for reasons that have nothing to do with
    the code, so the envelope is checked first and the source URI is the field
    that matters most.
    """
    checks.equal("%s.mode" % label, doc.get("mode"), mode)
    checks.equal("%s.success" % label, doc.get("success"), True)
    checks.equal("%s.source_uri" % label, doc.get("source_uri"), source_uri)
    checks.true("%s.elapsed_ms is present" % label, doc.get("elapsed_ms") is not None)
    return doc.get("result") or {}


def run_query(session, sql, max_rows):
    """
    One call to riskforge-execute-sql for the rows to compare on.

    `max_inline_rows` is 1 because this script never looks at a row: it needs the
    S3 key and the row count, and the rows themselves go from S3 into a DataFrame
    and never into a terminal. Only the shape of the result is printed.
    """
    lambda_client = session.client("lambda")
    print("querying riskforge-execute-sql")
    response = lambda_client.invoke(
        FunctionName="riskforge-execute-sql",
        Payload=json.dumps({"sql_query": sql, "max_rows": max_rows,
                            "max_inline_rows": 1}).encode("utf-8"),
    )
    payload = json.loads(response["Payload"].read().decode("utf-8"))
    if not payload.get("success"):
        raise SystemExit("query failed: %s" % payload.get("error"))
    print("  %d rows, %d columns, %s"
          % (payload["row_count"], len(payload["columns"]), payload["s3_uri"]))
    return payload["s3_uri"], payload["row_count"]

def in_process(mode, raw_df, source_uri, args):
    """
    The branch, run here, in the same envelope the task would have written.

    outputs.check is called on the envelope rather than skipped, because the task
    writes through it: a payload that passes the comparison but would have been
    refused on the way out is not a passing branch.
    """
    started = dt.datetime.now(dt.timezone.utc)
    if mode == "score":
        payload = credit.run(raw_df, args.pd_endpoint, args.lgd_endpoint,
                             batch_rows=args.batch_rows, workers=args.workers)
    else:
        payload = rates.run(raw_df)
    doc = outputs.envelope(mode, source_uri, payload, started, len(raw_df))
    outputs.check(doc)
    print("  in-process %s branch done in %d ms" % (mode, doc["elapsed_ms"]))
    return doc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--sql", default=DEFAULT_SQL)
    parser.add_argument("--max-rows", type=int, default=200000)
    parser.add_argument("--source-uri",
                        help="the query-result CSV both sides are computed from")
    parser.add_argument("--credit-uri", help="score-mode output of a real task run")
    parser.add_argument("--rates-uri", help="rates-mode output of a real task run")
    parser.add_argument("--pd-endpoint", default="riskforge-pd-endpoint")
    parser.add_argument("--lgd-endpoint", default="riskforge-lgd-endpoint")
    parser.add_argument("--batch-rows", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--only", choices=("score", "rates", "both"), default="both")
    args = parser.parse_args()

    # Set for the whole process, not just the session below: riskforge/scoring.py
    # builds its own sagemaker-runtime client per thread and has no session to be
    # handed one, by design -- in the task there is only the role.
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")

    want_score = args.only in ("score", "both")
    want_rates = args.only in ("rates", "both")

    # Named here rather than left to fail inside ExpectedLossTool: without these
    # two files there is nothing to compare the container against, and that is a
    # different problem from a mismatch.
    if want_score:
        for path in (config.PD_MODEL_PATH, config.LGD_MODEL_PATH):
            if not os.path.exists(path):
                raise SystemExit(
                    "%s is missing. This script needs the local models to have "
                    "something to compare the endpoints against." % path)

    source_uri = args.source_uri
    row_count = None
    if not source_uri:
        source_uri, row_count = run_query(session, args.sql, args.max_rows)

    print("loading rows")
    raw_df = inputs.load_query_result(source_uri, s3=s3)
    print("  %d rows, %d columns" % (len(raw_df), len(raw_df.columns)))

    checks = Checks()
    if row_count is not None:
        checks.equal("rows loaded match the query", len(raw_df), row_count)

    if want_score:
        print("score branch")
        if args.credit_uri:
            print("  reading %s" % args.credit_uri)
            doc = fetch_json(args.credit_uri, s3)
        else:
            doc = in_process("score", raw_df, source_uri, args)
        result = check_envelope(checks, "credit", doc, "score", source_uri)
        checks.equal("credit.row_count", doc.get("row_count"), len(raw_df))
        print("  comparing against the local models")
        compare_credit(checks, result, raw_df)

    if want_rates:
        print("rates branch")
        if args.rates_uri:
            print("  reading %s" % args.rates_uri)
            doc = fetch_json(args.rates_uri, s3)
        else:
            doc = in_process("rates", raw_df, source_uri, args)
        result = check_envelope(checks, "rates", doc, "rates", source_uri)
        checks.equal("rates.row_count", doc.get("row_count"), len(raw_df))
        print("  comparing against the local tools")
        compare_rates(checks, result, raw_df)

    print()
    print("source     %s" % source_uri)
    print("compared   %s" % (
        "the S3 output of a task run" if (args.credit_uri or args.rates_uri)
        else "the branches run in this process"))
    return checks.report()


if __name__ == "__main__":
    sys.exit(main())
