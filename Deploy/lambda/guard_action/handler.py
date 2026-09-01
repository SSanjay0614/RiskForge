"""
riskforge-guard-action -- the port of tools/schema_guard_tool.py.

Runs the Schema Guard prompt against one question and returns the three fields
of `dmodels/schema_guard_result.py`. No database, no VPC, no tool for the model
to call: one prompt in, one strict JSON object out.

Event:
    {"query": "What is our expected loss for California loans?"}

Response:
    {"success": true, "is_answerable": true,
     "reason": "One short sentence.", "requires_risk_analysis": true}

Errors are returned rather than raised -- {"success": false, "error": "..."} --
the same contract the other four functions use, so the Step Functions state
machine in Phase 11 branches on one field everywhere.

The defaults on a malformed response mirror the Pydantic model's and are
deliberately the cautious ones: `is_answerable` false (refuse rather than
generate SQL for a question the schema cannot support) and
`requires_risk_analysis` true (run the risk pipeline rather than silently return
a bare row count for a question about risk).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared"))

import gemini  # noqa: E402
from prompts import GUARD_PROMPT, SCHEMA_DESCRIPTION  # noqa: E402

# OpenAPI-subset schema, which is what the API accepts. `required` is what makes
# the difference between a constrained response and a suggestion: without it a
# model may return two of the three fields and leave the third to the defaults.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_answerable": {"type": "boolean"},
        "reason": {
            "type": "string",
            "description": "One short sentence explaining the decision.",
        },
        "requires_risk_analysis": {"type": "boolean"},
    },
    "required": ["is_answerable", "reason", "requires_risk_analysis"],
    "propertyOrdering": ["is_answerable", "reason", "requires_risk_analysis"],
}


def lambda_handler(event, context):
    query = (event or {}).get("query")
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "event must carry a non-empty 'query' string"}

    prompt = GUARD_PROMPT.format(schema=SCHEMA_DESCRIPTION, query=query.strip())

    try:
        parsed = gemini.generate_json(prompt, RESPONSE_SCHEMA, max_output_tokens=512)
    except gemini.GeminiError as exc:
        return {"success": False, "error": str(exc)}

    return {
        "success": True,
        "is_answerable": bool(parsed.get("is_answerable", False)),
        "reason": str(parsed.get("reason", "")),
        "requires_risk_analysis": bool(parsed.get("requires_risk_analysis", True)),
        "model": gemini.MODEL,
    }
