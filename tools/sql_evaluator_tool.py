import json
import re

import pandas as pd

from llm import ollama_provider

from tools.base_tool import BaseTool

from dmodels.evaluator_result import EvaluatorResult


# EVALUATOR_PROMPT = """A user asked this question: "{query}"

# This SQL query was run to answer it:
# {sql_query}

# It returned {row_count} rows. Sample of the results:
# {sample}

# Does this result plausibly answer the question? Consider whether the columns
# returned and the row count make sense given what was asked.

# Respond with ONLY a JSON object, no markdown fences, no explanation:
# {{"is_valid": true or false, "feedback": "one short sentence -- if invalid, explain what's wrong"}}
# """

EVALUATOR_PROMPT = """A user asked: "{query}"

SQL executed:
{sql_query}

Rows returned: {row_count}
Sample:
{sample}

Judge whether the SQL retrieved the correct underlying data needed to answer
the question.

IMPORTANT:
- SQL only retrieves and filters rows.
- SUM, COUNT, AVG, percentages, GROUP BY, and risk calculations happen
  downstream in Python.
- Therefore, do NOT reject a result because SQL did not perform aggregation.
- Extra columns are allowed.
- Reject only if the wrong rows, wrong filters, wrong fields, or unrelated
  data were returned.
- For calculation questions, check whether the returned rows contain the
  correct population and required fields.

Examples:
- "Total outstanding balance in CA" + CA loan rows with outstanding_balance
  -> VALID
- "How many B3 loans?" + B3 loan rows -> VALID
- "Average interest rate for debt consolidation" + debt_consolidation rows
  with int_rate -> VALID
- B3 question + A1 rows -> INVALID
- Loan question + employee records -> INVALID

Respond ONLY with:
{{"is_valid": true or false, "feedback": "one short sentence"}}
"""


def _parse_json_response(text: str) -> dict:

    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model response: {text!r}")

    return json.loads(match.group())


class SQLEvaluatorTool(BaseTool):
    """
    Judges whether a SQL query's results plausibly answer the original
    natural language question. Feeds a rejection's `feedback` back into
    TextToSQLTool for a corrected retry.

    Empty results are rejected deterministically without an LLM call --
    zero rows essentially always means a filter condition was wrong, so
    there's nothing for a judgment call to add over a fixed message.
    """

    def __init__(self):
        super().__init__("SQL Evaluator Tool")
        self.llm = ollama_provider.get_llm()

    def run(self, query: str, sql_query: str, rows_df: pd.DataFrame) -> EvaluatorResult:

        if rows_df is None or len(rows_df) == 0:
            return EvaluatorResult(
                is_valid=False,
                feedback="The query returned zero rows -- check filter conditions "
                         "and column names against the schema.",
            )

        sample = rows_df.head(5).to_string(index=False)

        prompt = EVALUATOR_PROMPT.format(
            query=query, sql_query=sql_query, row_count=len(rows_df), sample=sample
        )

        response = self.llm.invoke(prompt)
        parsed = _parse_json_response(response.content)

        return EvaluatorResult(
            is_valid=bool(parsed.get("is_valid", False)),
            feedback=str(parsed.get("feedback", "")),
        )
