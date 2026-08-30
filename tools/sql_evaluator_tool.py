import json
import re

import pandas as pd

from llm.ollama_provider import ollama_provider

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

Result profile (no raw rows are shown -- the shape of the result and the values
of the filtered columns only):
{profile}

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
- You are shown the distinct values of the columns the WHERE clause filtered
  on. Use those to confirm the filter did what the question asked: a question
  about California whose result profile shows addr_state = CA is correct.
- Do NOT reject a result merely because raw rows are not shown. They are
  withheld deliberately and their absence is not a defect.

These metrics are all computed downstream from the retrieved rows, so a
question asking for any of them is answered by retrieving the right population
-- never reject one as "external knowledge" or "not in the data":
probability of default, loss given default, exposure at default, expected
loss, risk-weighted assets, regulatory capital and the Basel III IRB capital
requirement, concentration (HHI), repricing gap, net interest income, net
interest margin, earnings at risk, and loan-to-deposit ratio.

Examples:
- "Total outstanding balance in CA" + rows filtered to addr_state = CA, with an
  outstanding_balance column present -> VALID
- "How many B3 loans?" + rows filtered to sub_grade = B3 -> VALID
- "Average interest rate for debt consolidation" + rows filtered to
  purpose = debt_consolidation, with an int_rate column present -> VALID
- "Regulatory capital requirement for loans under $10,000" + rows filtered to
  loan_amnt below 10000 -> VALID (the capital calculation runs downstream on
  exactly those rows)
- B3 question + a profile showing sub_grade = A1 -> INVALID
- Loan question + a profile whose columns are employee records -> INVALID

Respond ONLY with:
{{"is_valid": true or false, "feedback": "one short sentence"}}
"""

# Columns whose values never leave the process, even when a query filters on
# them: a primary key identifies a specific borrower's loan, and emp_title is
# free text a borrower typed about themselves. Their presence is reported; the
# values are not.
WITHHELD_COLUMNS = {"loan_id", "emp_title"}

# Above this many distinct values, a "distinct values" list stops describing a
# filter and starts being a data dump, so only the count is reported.
MAX_DISTINCT_LISTED = 12


def _parse_json_response(text: str) -> dict:

    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model response: {text!r}")

    return json.loads(match.group())


def _where_clause(sql_query: str) -> str:
    """The text of the WHERE clause, or "" if the query has none."""

    match = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql_query,
        re.IGNORECASE | re.DOTALL,
    )

    return match.group(1) if match else ""


def _filtered_columns(sql_query: str, columns) -> list:
    """Columns of the result that the WHERE clause actually mentions."""

    where_text = _where_clause(sql_query)
    if not where_text.strip():
        return []

    return [
        col
        for col in columns
        if re.search(rf"\b{re.escape(str(col))}\b", where_text, re.IGNORECASE)
    ]


def _describe_column(series: pd.Series) -> str:
    """
    One line describing a filtered column, chosen so a filter can be verified
    without any individual row being reproducible.
    """

    if series.name in WITHHELD_COLUMNS:
        return "present, values withheld (identifier or free text)"

    distinct = series.dropna().unique()

    if len(distinct) <= MAX_DISTINCT_LISTED:
        values = ", ".join(str(v) for v in sorted(distinct, key=str))
        return f"{len(distinct)} distinct value(s): {values}"

    if pd.api.types.is_numeric_dtype(series):
        return (
            f"{len(distinct)} distinct numeric values, "
            f"range {series.min()} to {series.max()}"
        )

    return f"{len(distinct)} distinct values (too many to list)"


def _result_profile(sql_query: str, rows_df: pd.DataFrame) -> str:
    """
    A description of the SHAPE of a result set, never its contents.

    The evaluator only needs to answer "did the right population come back?",
    which needs the row count, the available columns, and evidence that each
    filter did what the question asked. None of that requires a single raw
    row, and raw rows here are real borrowers' loan records -- so they are not
    sent. Values are reported only for the columns the WHERE clause filtered
    on, which are values the model already chose itself when it wrote the SQL,
    and even then identifiers and free text are withheld and high-cardinality
    columns collapse to a range or a count.
    """

    lines = [
        f"Rows returned: {len(rows_df)}",
        f"Columns returned ({len(rows_df.columns)}): "
        f"{', '.join(str(c) for c in rows_df.columns)}",
    ]

    filtered = _filtered_columns(sql_query, rows_df.columns)

    if not filtered:
        lines.append(
            "Filters: the query has no WHERE clause, so this is the full "
            "population."
        )
        return "\n".join(lines)

    lines.append("Values of the columns the WHERE clause filtered on:")
    lines.extend(f"  - {col}: {_describe_column(rows_df[col])}" for col in filtered)

    return "\n".join(lines)


class SQLEvaluatorTool(BaseTool):
    """
    Judges whether a SQL query's results plausibly answer the original
    natural language question. Feeds a rejection's `feedback` back into
    TextToSQLTool for a corrected retry.

    Empty results are rejected deterministically without an LLM call --
    zero rows essentially always means a filter condition was wrong, so
    there's nothing for a judgment call to add over a fixed message.

    The model is shown a profile of the result, never the rows themselves --
    see `_result_profile()`.
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

        prompt = EVALUATOR_PROMPT.format(
            query=query,
            sql_query=sql_query,
            profile=_result_profile(sql_query, rows_df),
        )

        response = self.llm.invoke(prompt)
        parsed = _parse_json_response(response.content)

        return EvaluatorResult(
            is_valid=bool(parsed.get("is_valid", False)),
            feedback=str(parsed.get("feedback", "")),
        )
