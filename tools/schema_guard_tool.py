import json
import re

from llm import ollama_provider

from tools.base_tool import BaseTool

from dmodels.schema_guard_result import SchemaGuardResult

from Database.schema_description import SCHEMA_DESCRIPTION




GUARD_PROMPT = """You are the Schema Guard for a Text-to-SQL system.

Your job is to determine whether the user's question can be answered using
ONLY the information represented by the database schema below.

DATABASE SCHEMA:
{schema}

USER QUESTION:
"{query}"

Determine whether the schema contains all the concepts and fields needed to
construct a SQL query that could answer the question.
IMPORTANT CONTEXT -- DOWNSTREAM COMPUTED METRICS:
This system does not stop at SQL retrieval. After relevant rows are retrieved,
downstream models and Python calculations compute additional risk metrics
from the raw loan/borrower data, including:
- Probability of Default (PD) and risk tier, from a trained behavioral PD model
- Loss Given Default (LGD), from a trained LGD model
- Expected Loss (EL = PD x LGD x EAD), combining the above with each loan's
  outstanding balance
- Portfolio concentration (HHI), by sector (purpose) or region (addr_state)
- Interest rate repricing gap, from each loan's term and issue date

Treat questions about these metrics (expected loss, PD, default probability,
risk tier, concentration, HHI, repricing gap, interest rate risk, etc.) as
ANSWERABLE, as long as the underlying raw loan/borrower data needed to
compute them is present in the schema below (it is). Do NOT mark these
unanswerable just because they are not literal column names -- they are
computed downstream by models and tools, not retrieved directly via SQL.
IMPORTANT RULES:

1. Mark is_answerable=true if the requested information can be obtained
   directly from the available columns or derived from them using normal SQL
   operations such as:
   - filtering
   - grouping
   - sorting
   - COUNT, SUM, AVG, MIN, MAX
   - percentages and ratios
   - comparisons
   - arithmetic calculations
   - date/time filtering, if the required date fields exist
   - joins, if the required relationships are represented in the schema

2. Do NOT require the schema to contain a column whose name exactly matches
   the wording of the question. Derived metrics are allowed when their
   underlying fields are available.

3. Mark is_answerable=false when the question requires a concept, attribute,
   relationship, or time dimension that is completely absent from the schema.

4. Do NOT mark a question as unanswerable merely because:
   - the question is complex,
   - the answer requires aggregation,
   - the requested value is not stored directly,
   - the database may contain zero matching rows,
   - the wording differs from the column names.

5. Judge only whether the QUESTION is structurally answerable from the schema.
   Do NOT judge whether matching records actually exist in the database.
   Do NOT generate SQL.

6. If the question asks about a concept unrelated to the schema
   (for example, stock prices when no market-price data exists, weather,
   employee information when no employee data exists, or another completely
   unrelated domain), mark it false.

7. If even one essential piece of information required to answer the question
   is missing from the schema, mark it false.
   
8. Do NOT mark a question false merely because it asks for a computed risk
   metric (expected loss, PD, LGD, risk tier, HHI, concentration, repricing
   gap) rather than a raw column value -- see DOWNSTREAM COMPUTED METRICS
   above.
   
9. Additionally, determine whether answering this question requires computing
   risk metrics (expected loss, PD, LGD, concentration/HHI, regulatory capital)
   on top of the retrieved data, or whether it's answerable directly from the
   raw rows themselves (a count, a list, a simple lookup/filter). Set
   requires_risk_analysis=true only if genuine risk computation is needed.

   Examples:
   "How many loans have sub_grade B3?" -> requires_risk_analysis: false
   "List loans in California" -> requires_risk_analysis: false
   "What is our expected loss for California loans?" -> requires_risk_analysis: true
   "What is our concentration risk by sector?" -> requires_risk_analysis: true

Examples:

Question: "What is the average loan amount in California?"
Schema contains loan_amnt and addr_state.
-> true

Question: "What percentage of loans are charged off?"
Schema contains loan_status.
-> true

Question: "What was the loan exposure in 2025?"
Schema contains loan amount but no date/year field.
-> false

Question: "How many loans are in Wyoming?"
Schema contains addr_state.
-> true
(Even if the database contains zero Wyoming loans.)

Question: "What is the current market price of our loans?"
Schema contains loan information but no market-price information. 
-> false

Return ONLY a valid JSON object with no markdown, explanation, or additional text.

Exact format:
{{"is_answerable": true, "reason": "The schema contains the fields needed to answer the question.", "requires_risk_analysis": true}}

The reason must be ONE short sentence.
"""

def _parse_json_response(text: str) -> dict:
    """Local models sometimes wrap JSON in markdown fences or add stray text
    around it -- strip defensively rather than trusting raw output."""

    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model response: {text!r}")

    return json.loads(match.group())


class SchemaGuardTool(BaseTool):
    """
    Fast, cheap check run before any SQL generation is attempted: is this
    query structurally answerable against the known schema at all? Lets the
    Data Agent short-circuit obviously unanswerable queries without wasting
    a retry cycle on something impossible to answer.
    """

    def __init__(self):
        super().__init__("Schema Guard Tool")
        self.llm = ollama_provider.get_llm()

    def run(self, query: str) -> SchemaGuardResult:

        prompt = GUARD_PROMPT.format(schema=SCHEMA_DESCRIPTION, query=query)

        response = self.llm.invoke(prompt)
        parsed = _parse_json_response(response.content)

        return SchemaGuardResult(
            is_answerable=bool(parsed.get("is_answerable", False)),
            reason=str(parsed.get("reason", "")),
            requires_risk_analysis=bool(parsed.get("requires_risk_analysis", True)),
        )
