"""
riskforge-sqlgen-action -- the port of tools/text_to_sql_tool.py.

Generates one PostgreSQL SELECT for a question, optionally with the evaluator's
feedback from a rejected attempt, and returns the two fields of
`dmodels/sql_generation_result.py`.

Event:
    {"query": "Total outstanding balance in California",
     "feedback": "optional -- the evaluator's reason for rejecting the last try"}

Response:
    {"success": true, "sql_query": "SELECT ...", "is_select": true}

This function does NOT enforce read-only, and nothing here should be mistaken
for that. `is_select` is a fast label on the generated text, the same
pre-check the local tool made; the boundary is the `riskforge_ro` role's grants
and `default_transaction_read_only`, enforced by PostgreSQL when
riskforge-execute-sql runs the statement. See Deploy/lambda/sql/create_readonly_role.sql
and the adversarial checks in Deploy/lambda/test_functions.py.

Asking for `{"sql_query": "..."}` rather than raw text is what removes the
markdown-fence problem: with a response schema the model returns a JSON string
field, not a fenced code block. `_clean_sql` is kept anyway, because a model that
puts a fence *inside* the string is a thing that happens and the fix costs two
regexes.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared"))

import gemini  # noqa: E402
from prompts import (  # noqa: E402
    FEEDBACK_BLOCK_TEMPLATE,
    SCHEMA_DESCRIPTION,
    SQL_GENERATION_PROMPT,
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sql_query": {
            "type": "string",
            "description": "A single PostgreSQL SELECT statement, no trailing semicolon.",
        }
    },
    "required": ["sql_query"],
}


def _clean_sql(text):
    """Carried across from tools/text_to_sql_tool.py unchanged."""
    text = text.strip()
    text = re.sub(r"^```(sql)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()
    text = text.rstrip(";").strip()
    return text


def lambda_handler(event, context):
    event = event or {}
    query = event.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "event must carry a non-empty 'query' string"}

    feedback = event.get("feedback")
    feedback_block = (
        FEEDBACK_BLOCK_TEMPLATE.format(feedback=feedback)
        if isinstance(feedback, str) and feedback.strip()
        else ""
    )

    prompt = SQL_GENERATION_PROMPT.format(
        schema=SCHEMA_DESCRIPTION, query=query.strip(), feedback_block=feedback_block
    )

    try:
        parsed = gemini.generate_json(prompt, RESPONSE_SCHEMA, max_output_tokens=1024)
    except gemini.GeminiError as exc:
        return {"success": False, "error": str(exc)}

    sql_query = _clean_sql(str(parsed.get("sql_query", "")))

    return {
        "success": True,
        "sql_query": sql_query,
        "is_select": bool(re.match(r"(?is)^\s*SELECT\b", sql_query)),
        "retry": bool(feedback_block),
        "model": gemini.MODEL,
    }
