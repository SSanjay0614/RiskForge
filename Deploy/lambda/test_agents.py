"""
Exercises the three deployed prompt Lambdas, including the parts that are
supposed to fail.

    python Deploy/lambda/test_agents.py --profile riskforge

Three things make this more than a smoke test.

  * **Every check asserts a decision, never wording.** A model call is
    nondeterministic in a way a database call is not. "is_answerable is false for
    a question about FX exposure" is a claim about the schema and will hold;
    "the reason mentions foreign exchange" is a claim about a sentence the model
    happened to write this time. temperature is 0 in shared/gemini.py, so a
    decision that flips between two runs is a finding, not flakiness.
  * **The generated SQL is executed, not read.** Each statement goes through the
    real riskforge-execute-sql function against the real database. Invalid
    PostgreSQL, a quoted identifier, a column that does not exist and a join on
    the wrong key all look fine to a human skimming a SELECT, and all of them
    fail here.
  * **The evaluator is handed an event carrying a `rows` key, and must refuse
    it**, and is separately handed a loan_id filter summary full of identifiers,
    which must not appear in what the model was shown. Those two are the checks
    that the profile-only boundary is enforced by the function rather than
    promised in its docstring.

The helpers below are deliberately copied from test_functions.py rather than
imported: that module parses argv at import time, and a test suite that cannot be
run standalone is a test suite people stop running.
"""
import argparse
import json
import re
import sys
import time

import boto3

results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name, (" -- " + detail) if detail else ""))


# Seconds between invocations. The free tier meters model calls per minute, and
# this suite fires roughly twenty of them back to back, so without pacing the run
# dies on 429 partway through and every later check "fails" for a reason that has
# nothing to do with the code. gemini.py's own retry backs off ~2s and ~4s, which
# clears a burst but not a per-minute ceiling.
#
# Crude on purpose: it sleeps before every invocation, including the dozen
# evaluator events that are refused before any model call. Tracking which
# invocations spend quota would be a second model of the handlers' control flow
# living in the test, and being wrong about it is how a test suite starts lying.
PACE_S = 6.5


def invoke(client, function_name, payload):
    if PACE_S:
        time.sleep(PACE_S)
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


def _retry_seconds(error, default=65.0, cap=180.0):
    """The wait a 429 asked for, padded, or a whole quota window if it did not say."""
    match = re.search("retry in ([0-9.]+)s", error, re.I)
    seconds = float(match.group(1)) if match else default
    return min(seconds + 2.0, cap)


def preflight(client, guard):
    """
    One call, before any assertion, so a missing API key reads as a missing API
    key instead of forty mysterious failures. The three functions share
    shared/gemini.py, so whatever is wrong with the model call is wrong for all
    three and there is nothing to learn from watching it fail forty times.

    A 429 is waited out rather than reported. The free tier meters 20 requests per
    minute per model, and a previous run of this suite -- or a debugging session
    against the same key -- leaves the window spent, which is a state that clears
    itself in under a minute and says nothing about whether the functions work.
    gemini.py cannot wait it out from inside the Lambda: the window can be 60s
    away and the invocation timeout is 90s, so the wait belongs on the client that
    is not paying for it.
    """
    for attempt in (1, 2):
        out = invoke(client, guard, {"query": "How many loans are there?"})
        if out.get("success"):
            return
        error = out.get("error") or ""

        if "429" in error and attempt == 1:
            wait = _retry_seconds(error)
            print("  model quota window is spent; waiting %.0fs for it to clear" % wait)
            time.sleep(wait)
            continue
        break

    hint = ""
    # Matched on the provider's wording for a rejected credential, not on the
    # status code. "HTTP 400" was the first version of this check and it was
    # wrong: an unset key and an unsupported request field are both 400s, so it
    # printed "set the real key" at someone who had already set it.
    if ("API_KEY_INVALID" in error or "API key not valid" in error
            or "PLACEHOLDER" in error):
        hint = (
            "\n\nThe parameter still holds Terraform's placeholder, or the key is not valid:\n"
            "  MSYS_NO_PATHCONV=1 aws ssm put-parameter --name /riskforge/gemini-api-key "
            "--type SecureString --value <key> --overwrite --profile riskforge"
        )
    elif "404" in error and "models/" in error:
        hint = (
            "\n\nThe model name is wrong, or unavailable to this key. It is the"
            " GEMINI_MODEL environment variable, set from var.gemini_model in"
            " infra/variables.tf -- change it there and re-apply. No rebuild:"
            " the model name is configuration, not code."
        )
    elif "429" in error:
        hint = (
            "\n\nStill rate-limited after waiting. Either something else is using the"
            " same key, or the daily cap is spent -- check https://ai.dev/rate-limit."
        )
    raise SystemExit("the model call is not working, so nothing below would prove "
                     "anything:\n  %s%s" % (error, hint))



# (question, is_answerable, requires_risk_analysis). The two False rows name
# things the schema genuinely cannot supply -- there is no currency column
# anywhere, and a liquidity coverage ratio needs high-quality liquid assets and
# 30-day outflows, neither of which is a loan attribute. A guard that waves those
# through hands text-to-SQL a question with no correct answer, and the pipeline
# then invents one.
GUARD_CASES = [
    ("What is our expected loss for California loans?", True, True),
    ("How many loans are in sub-grade B3?", True, False),
    ("How risky is our Texas loan book?", True, True),
    ("What is the average interest rate by loan purpose?", True, False),
    ("What is our net FX exposure by currency?", False, None),
    ("What is our liquidity coverage ratio?", False, None),
    # Not a missing concept but a missing outcome: there is no loan_status column
    # and the schema Notes say the portfolio is active and unresolved, so no
    # charge-off, default or payoff is recorded. The guard has to refuse this one
    # on the strength of a Note rather than an absent word, which is the harder
    # case and the one a reviewer will ask about.
    ("How many charged-off loans were issued in 2017?", False, None),
]


def test_guard(client, guard):
    print("guard-action")

    for question, answerable, needs_risk in GUARD_CASES:
        out = invoke(client, guard, {"query": question})
        if not out.get("success"):
            record("guard: %s" % question[:44], False, out.get("error", "")[:110])
            continue

        got = bool(out.get("is_answerable"))
        record(
            "is_answerable=%-5s  %s" % (str(answerable).lower(), question[:44]),
            got == answerable,
            out.get("reason", "")[:100],
        )

        # Only meaningful on the answerable ones: the prompt does not constrain
        # requires_risk_analysis when it has already refused the question.
        if answerable and needs_risk is not None:
            record(
                "requires_risk_analysis=%-5s  %s" % (str(needs_risk).lower(), question[:36]),
                bool(out.get("requires_risk_analysis")) == needs_risk,
                "got %s" % out.get("requires_risk_analysis"),
            )

    out = invoke(client, guard, {"query": "   "})
    record(
        "empty query rejected without a model call",
        not out.get("success") and "query" in (out.get("error") or ""),
        out.get("error", "")[:90],
    )


# What SQL_GENERATION_PROMPT actually asks for, which is the opposite of what a
# reader expects. Rules 2, 3, 4 and 5 forbid aggregation in SQL and mandate
# SELECT * over the full loans-borrowers join: retrieval happens in PostgreSQL,
# and the aggregation plus the PD/LGD/EL maths happen downstream in Python on the
# frame. A generated COUNT(*) is therefore a bug, not a success -- the first
# version of this suite had it backwards and "failed" three times on correct
# output.
#
# (question, strings the WHERE clause must carry, whether there should be a WHERE)
SQLGEN_CASES = [
    ("What is the total outstanding balance of loans in California?",
     ["addr_state", "'CA'"], True),
    # No filter is implied by "for each grade", and rule 8 says return all rows
    # without a WHERE clause. The absence is the assertion.
    ("What is the average interest rate for each loan grade?",
     [], False),
    # Two filters, and the date one is the point: rule 19 exists because the
    # migration kept issue_date as ISO TEXT, so '2017-01-01' <= issue_date is
    # correct and EXTRACT or SQLite's strftime is a type error. The first draft of
    # this case asked for charged-off loans in 2017 and the model returned no
    # WHERE clause at all -- correctly, since there is no loan_status column. That
    # question moved to GUARD_CASES, where refusing it is the right answer.
    ("Which 60-month loans were issued in 2017?",
     ["term", "2017"], True),
]

# Written without backslash classes so the patterns survive being read aloud in a
# review: [(] is a literal paren, " +" is one-or-more spaces.
AGGREGATE_RE = re.compile("(sum|count|avg|min|max) *[(]", re.I)
GROUP_BY_RE = re.compile("group +by", re.I)
SELECT_STAR_RE = re.compile("^ *select +[*]", re.I)
JOIN_RE = re.compile("join +borrowers +using *[(] *loan_id *[)]", re.I)
WHERE_RE = re.compile("(^| )where( |$)", re.I)


def test_sqlgen(client, sqlgen, execute_sql):
    print("sqlgen-action")

    for question, must_contain, expect_where in SQLGEN_CASES:
        out = invoke(client, sqlgen, {"query": question})
        if not out.get("success"):
            record("sqlgen: %s" % question[:40], False, out.get("error", "")[:110])
            continue

        sql = out.get("sql_query") or ""
        label = question[:34]

        record("starts with SELECT: %s" % label, bool(out.get("is_select")), sql[:90])
        record("no quoted identifiers: %s" % label, chr(34) not in sql, sql[:90])
        record("no trailing semicolon: %s" % label, ";" not in sql, "")
        record("no aggregate, rule 3: %s" % label,
               AGGREGATE_RE.search(sql) is None, sql[:90])
        record("no GROUP BY, rule 4: %s" % label,
               GROUP_BY_RE.search(sql) is None, sql[:90])
        record("full loans-borrowers join, rule 2: %s" % label,
               SELECT_STAR_RE.search(sql) is not None and JOIN_RE.search(sql) is not None,
               sql[:90])

        # strftime is SQLite's, and EXTRACT on a TEXT column is a PostgreSQL type
        # error. Both are what rule 19 was written to prevent, so both are checked
        # on every statement rather than only the one asking about a year.
        record("no strftime or EXTRACT, rule 19: %s" % label,
               "strftime" not in sql.lower() and "extract(" not in sql.lower().replace(" ", ""),
               sql[:90])
        record("WHERE %s, rules 7-8: %s" % ("present" if expect_where else "absent", label),
               bool(WHERE_RE.search(sql)) == expect_where, sql[:90])
        for needle in must_contain:
            record("filter carries %s: %s" % (needle, label),
                   needle.lower() in sql.lower(), sql[:90])

        # PostgreSQL has to be the one that accepts it: a quoted identifier is
        # valid-looking SQL that only fails on the server, which is why reading
        # the string is not enough.
        #
        # Wrapped in LIMIT 1 rather than run as written. Rule 2 mandates every
        # column of an 878k-row join, so the statement is deliberately unbounded
        # and running it to completion hits db.py's 25s statement_timeout -- which
        # says nothing about whether the SQL was correct. Planning one row still
        # resolves every table, column, join and grant, and that is the whole
        # claim. The wrapper is a single SELECT, so execute_sql's read-only
        # validator is not being sidestepped; Phase 10's Fargate task is what
        # actually drains the population.
        probe = "SELECT * FROM (%s) AS generated LIMIT 1" % sql
        ran = invoke(client, execute_sql, {"sql_query": probe})
        record(
            "PostgreSQL accepted it: %s" % label,
            bool(ran.get("success")),
            (ran.get("error") or ("row_count=%s" % ran.get("row_count")))[:120],
        )


    # Feedback is the evaluator's rejection fed back in, and a retry that returns
    # the same statement is a retry that will be rejected again. The check is that
    # the correction lands, not that the wording changed.
    question = "What is the total outstanding balance by state?"
    first = invoke(client, sqlgen, {"query": question})
    retry = invoke(client, sqlgen, {
        "query": question,
        "feedback": "The query returned every state. The question is only about Texas, "
                    "so filter addr_state to 'TX'.",
    })
    record("retry flag set when feedback is supplied", bool(retry.get("retry")), "")
    record(
        "feedback changes the statement",
        first.get("sql_query") != retry.get("sql_query"),
        (retry.get("sql_query") or "")[:100],
    )
    record(
        "feedback applied the filter it asked for",
        "TX" in (retry.get("sql_query") or ""),
        (retry.get("sql_query") or "")[:100],
    )


def test_evaluator(client, evaluator):
    print("evaluator-action")

    out = invoke(client, evaluator, {
        "query": "What is the total outstanding balance of loans in California?",
        "sql_query": "SELECT SUM(out_prncp) AS total FROM loans WHERE addr_state = 'CA'",
        "profile": {
            "row_count": 1,
            "columns": ["total"],
            "filters": [{"column": "addr_state", "summary": "1 distinct value(s): CA"}],
        },
    })
    record("matching profile accepted", bool(out.get("is_valid")), out.get("feedback", "")[:100])
    record("model was called", bool(out.get("model_called")), "")
    record(
        "profile_sent echoes what the model saw",
        "addr_state" in (out.get("profile_sent") or ""),
        (out.get("profile_sent") or "").replace("\n", " | ")[:110],
    )

    # The filter is applied, the SQL is valid and the row count is plausible --
    # and the population is the wrong one. This is the only failure mode the
    # evaluator exists for; the rest of the pipeline already catches the others.
    out = invoke(client, evaluator, {
        "query": "How many loans are in sub-grade B3?",
        "sql_query": "SELECT count(*) AS n FROM loans WHERE sub_grade = 'A1'",
        "profile": {
            "row_count": 1,
            "columns": ["n"],
            "filters": [{"column": "sub_grade", "summary": "1 distinct value(s): A1"}],
        },
    })
    record("wrong population rejected", not out.get("is_valid"), out.get("feedback", "")[:110])
    record(
        "rejection carries feedback sqlgen can act on",
        bool((out.get("feedback") or "").strip()),
        "",
    )

    # Zero rows is decided in code. Asserting model_called is false asserts the
    # pipeline does not pay for a model call to be told what an empty result
    # already means.
    out = invoke(client, evaluator, {
        "query": "How many loans are in sub-grade Z9?",
        "sql_query": "SELECT count(*) AS n FROM loans WHERE sub_grade = 'Z9'",
        "profile": {"row_count": 0, "columns": ["n"], "filters": []},
    })
    record(
        "zero rows rejected with no model call",
        out.get("success") and out.get("is_valid") is False and out.get("model_called") is False,
        out.get("feedback", "")[:90],
    )

    # The boundary. A caller being helpful -- "here are the rows too, in case it
    # helps judge" -- is the realistic way borrower data reaches a third-party
    # model, so it has to fail at the function rather than be caught in review.
    out = invoke(client, evaluator, {
        "query": "Loans in California",
        "sql_query": "SELECT * FROM loans WHERE addr_state = 'CA'",
        "profile": {"row_count": 2, "columns": ["loan_id"], "filters": []},
        "rows": [[1077501, 5000.0], [1077430, 2500.0]],
    })
    record(
        "top-level rows key refused",
        not out.get("success") and "row data" in (out.get("error") or ""),
        out.get("error", "")[:110],
    )

    out = invoke(client, evaluator, {
        "query": "Loans in California",
        "sql_query": "SELECT * FROM loans WHERE addr_state = 'CA'",
        "profile": {
            "row_count": 2,
            "columns": ["loan_id"],
            "filters": [],
            "sample": {"loan_id": [1077501, 1077430]},
        },
    })
    record(
        "nested sample key refused, with its path named",
        not out.get("success") and "profile.sample" in (out.get("error") or ""),
        out.get("error", "")[:110],
    )

    # A declared filter list can claim a filter the query never applied, which
    # would have the model confirm a population that was never retrieved. Dropped
    # rather than trusted, and reported rather than dropped silently.
    out = invoke(client, evaluator, {
        "query": "Charged-off loans in California",
        "sql_query": "SELECT count(*) AS n FROM loans WHERE addr_state = 'CA'",
        "profile": {
            "row_count": 1,
            "columns": ["n"],
            "filters": [
                {"column": "addr_state", "summary": "1 distinct value(s): CA"},
                {"column": "loan_status", "summary": "1 distinct value(s): Charged Off"},
            ],
        },
    })
    record(
        "filter absent from the WHERE clause is dropped",
        out.get("filters_ignored") == ["loan_status"],
        "filters_ignored=%s" % out.get("filters_ignored"),
    )
    record(
        "the dropped filter never reached the model",
        "loan_status" not in (out.get("profile_sent") or ""),
        "",
    )

    # loan_id is a primary key, so a summary of it is a list of identified loans.
    # The handler overwrites the summary it was handed rather than trusting the
    # caller to have withheld it -- the identifiers were sent in and must not
    # appear in what the model was shown.
    out = invoke(client, evaluator, {
        "query": "Amounts for these three loans",
        "sql_query": "SELECT loan_amnt FROM loans WHERE loan_id IN (1077501, 1077430, 1077175)",
        "profile": {
            "row_count": 3,
            "columns": ["loan_amnt"],
            "filters": [{"column": "loan_id",
                         "summary": "3 distinct value(s): 1077501, 1077430, 1077175"}],
        },
    })
    sent = out.get("profile_sent") or ""
    record("loan_id summary withheld", "values withheld" in sent, "")
    record("no identifier reached the model", "1077501" not in sent, sent.replace("\n", " | ")[:110])

    # "summary" is where 878k values would fit if nobody was looking.
    out = invoke(client, evaluator, {
        "query": "Loans in California",
        "sql_query": "SELECT count(*) AS n FROM loans WHERE addr_state = 'CA'",
        "profile": {
            "row_count": 1,
            "columns": ["n"],
            "filters": [{"column": "addr_state", "summary": "CA " + ("1077501, " * 200)}],
        },
    })
    sent = out.get("profile_sent") or ""
    record(
        "oversized summary truncated",
        "(truncated)" in sent and len(sent) < 700,
        "profile_sent is %d chars" % len(sent),
    )

    for bad, why in [
        ({"sql_query": "SELECT 1", "profile": {"row_count": 1}}, "query"),
        ({"query": "x", "profile": {"row_count": 1}}, "sql_query"),
        ({"query": "x", "sql_query": "SELECT 1"}, "profile"),
        ({"query": "x", "sql_query": "SELECT 1", "profile": {"row_count": -3}}, "row_count"),
    ]:
        out = invoke(client, evaluator, bad)
        record(
            "malformed event rejected: missing/bad %s" % why,
            not out.get("success") and why in (out.get("error") or ""),
            out.get("error", "")[:80],
        )


def main():
    global PACE_S

    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--guard", default="riskforge-guard-action")
    parser.add_argument("--sqlgen", default="riskforge-sqlgen-action")
    parser.add_argument("--evaluator", default="riskforge-evaluator-action")
    parser.add_argument("--execute-sql", default="riskforge-execute-sql")
    parser.add_argument(
        "--pace", type=float, default=PACE_S,
        help="seconds between invocations; the free-tier model quota is per minute. "
             "0 runs flat out, which is what a paid key can do.",
    )
    args = parser.parse_args()

    PACE_S = args.pace

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("lambda")

    preflight(client, args.guard)
    test_guard(client, args.guard)
    test_sqlgen(client, args.sqlgen, args.execute_sql)
    test_evaluator(client, args.evaluator)

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print("%d of %d checks failed:" % (len(failed), len(results)))
        for name in failed:
            print("  - %s" % name)
        sys.exit(1)
    print("all %d checks passed" % len(results))


if __name__ == "__main__":
    main()
