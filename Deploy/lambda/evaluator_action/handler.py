"""
riskforge-evaluator-action -- the port of tools/sql_evaluator_tool.py.

Judges whether a query retrieved the right population, and returns the two
fields of `dmodels/evaluator_result.py`. On a rejection, `feedback` is what
riskforge-sqlgen-action takes as its `feedback` input for a corrected retry.

Event:
    {"query": "Total outstanding balance in California",
     "sql_query": "SELECT * FROM loans JOIN borrowers USING(loan_id) WHERE addr_state = 'CA'",
     "profile": {"row_count": 72451,
                 "columns": ["loan_id", "loan_amnt", ...],
                 "filters": [{"column": "addr_state", "summary": "1 distinct value(s): CA"}]}}

Response:
    {"success": true, "is_valid": true, "feedback": "one short sentence",
     "model_called": true, "profile_sent": "<exactly what the model was shown>"}

**This function will not accept rows, and that is the whole point of it.** The
local tool built the profile itself from the DataFrame it was handed, so "the
model never sees a row" was guaranteed by the code that called the model. Here
the caller holds the frame (the Fargate task in Phase 10) and passes a profile
across a network boundary, which turns a guarantee into a promise. Three things
turn it back into a guarantee:

  * A recursive scan rejects the whole event if any key anywhere in it is a
    row-carrying name -- `rows`, `sample`, `records`, `data`. A caller that tries
    to be helpful gets an error, not a leak.
  * `loan_id` and `emp_title` summaries are overwritten here, not trusted.
    Carried across from WITHHELD_COLUMNS: a primary key identifies one
    borrower's loan and emp_title is free text a borrower typed about
    themselves.
  * Every summary is length-capped, because "summary" is where 878k loan_ids
    would fit if nobody was looking.

And the response echoes `profile_sent` -- the exact text handed to the model --
so what was disclosed is auditable from the caller's side rather than a claim
made in a docstring.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared"))

import gemini  # noqa: E402
from prompts import EMPTY_RESULT_FEEDBACK, EVALUATOR_PROMPT  # noqa: E402

# Carried across from tools/sql_evaluator_tool.py.
WITHHELD_COLUMNS = {"loan_id", "emp_title"}
WITHHELD_SUMMARY = "present, values withheld (identifier or free text)"

# Any of these appearing as a key anywhere in the event is treated as an attempt
# to send row data, whatever it actually holds.
BANNED_KEYS = {
    "rows", "row", "sample", "samples", "records", "record",
    "data", "preview", "head", "values", "raw",
}

# A filter summary describes a filter. Past this length it is carrying something
# else.
MAX_SUMMARY_CHARS = 200

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_valid": {"type": "boolean"},
        "feedback": {"type": "string", "description": "One short sentence."},
    },
    "required": ["is_valid", "feedback"],
    "propertyOrdering": ["is_valid", "feedback"],
}

def _banned_key(node, path="event"):
    """The path of the first row-carrying key anywhere in the event, or None."""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in BANNED_KEYS:
                return "%s.%s" % (path, key)
            found = _banned_key(value, "%s.%s" % (path, key))
            if found:
                return found
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            found = _banned_key(value, "%s[%d]" % (path, index))
            if found:
                return found
    return None


def _where_clause(sql_query):
    """Carried across from tools/sql_evaluator_tool.py unchanged."""
    match = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql_query,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _render_profile(sql_query, profile):
    """
    The prompt's profile block, plus the filters that were declared but do not
    appear in the query's WHERE clause.

    Checking the declared filters against the SQL is the part that has to happen
    here: the local tool derived them from the query itself and so could not be
    lied to, whereas a declared list can claim a filter that was never applied
    -- which would have the model confirm a population the query never
    retrieved.
    """
    row_count = profile.get("row_count")
    columns = [str(c) for c in (profile.get("columns") or [])]

    lines = [
        "Rows returned: %s" % row_count,
        "Columns returned (%d): %s" % (len(columns), ", ".join(columns)),
    ]

    where_text = _where_clause(sql_query)
    kept, ignored = [], []

    for entry in profile.get("filters") or []:
        if not isinstance(entry, dict):
            continue
        column = str(entry.get("column", "")).strip()
        if not column:
            continue
        if not re.search(r"\b%s\b" % re.escape(column), where_text, re.IGNORECASE):
            ignored.append(column)
            continue

        if column in WITHHELD_COLUMNS:
            summary = WITHHELD_SUMMARY
        else:
            summary = str(entry.get("summary", "")).strip()
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS] + " ... (truncated)"
        kept.append((column, summary))

    if not kept:
        lines.append(
            "Filters: the query has no WHERE clause, so this is the full population."
            if not where_text.strip()
            else "Filters: the query has a WHERE clause but no column summaries "
                 "were supplied."
        )
    else:
        lines.append("Values of the columns the WHERE clause filtered on:")
        lines.extend("  - %s: %s" % (column, summary) for column, summary in kept)

    return "\n".join(lines), ignored


def lambda_handler(event, context):
    event = event or {}

    leaked = _banned_key(event)
    if leaked:
        return {
            "success": False,
            "error": "refusing the event: %s looks like row data, and this function "
                     "is only ever given a result profile" % leaked,
        }

    query = event.get("query")
    sql_query = event.get("sql_query")
    profile = event.get("profile")

    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "event must carry a non-empty 'query' string"}
    if not isinstance(sql_query, str) or not sql_query.strip():
        return {"success": False, "error": "event must carry a non-empty 'sql_query' string"}
    if not isinstance(profile, dict):
        return {"success": False, "error": "event must carry a 'profile' object"}

    row_count = profile.get("row_count")
    if not isinstance(row_count, int) or row_count < 0:
        return {
            "success": False,
            "error": "profile.row_count must be a non-negative integer",
        }

    # Zero rows is decided here, with no model call: it essentially always means a
    # filter was wrong, and a judgement call adds nothing to a fixed message.
    # Carried across from SQLEvaluatorTool.run(), where it was the same shortcut.
    if row_count == 0:
        return {
            "success": True,
            "is_valid": False,
            "feedback": EMPTY_RESULT_FEEDBACK,
            "model_called": False,
        }

    profile_text, ignored = _render_profile(sql_query, profile)

    prompt = EVALUATOR_PROMPT.format(
        query=query.strip(), sql_query=sql_query.strip(), profile=profile_text
    )

    try:
        parsed = gemini.generate_json(prompt, RESPONSE_SCHEMA, max_output_tokens=512)
    except gemini.GeminiError as exc:
        return {"success": False, "error": str(exc)}

    return {
        "success": True,
        "is_valid": bool(parsed.get("is_valid", False)),
        "feedback": str(parsed.get("feedback", "")),
        "model_called": True,
        # What the model was actually shown, so the caller can audit the
        # disclosure rather than trust this function's docstring.
        "profile_sent": profile_text,
        "filters_ignored": ignored,
        "model": gemini.MODEL,
    }

