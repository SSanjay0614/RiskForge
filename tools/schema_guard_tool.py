import json
import re

from llm.ollama_provider import ollama_provider

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
- Net interest income, net interest margin and earnings at risk, from each
  loan's interest rate and a documented deposit assumption
- Regulatory capital under Basel III: asset correlation (R), the capital
  requirement factor (K), risk-weighted assets (RWA) and the 8% capital
  reserve, computed from the modelled PD and LGD plus each loan's outstanding
  balance using the Basel IRB Other Retail risk-weight formula
- Compliance checks of the above against the thresholds in Risk_Limits

Treat questions about these metrics (expected loss, PD, default probability,
risk tier, credit quality, risk profile, concentration, HHI, repricing gap,
interest rate risk, net interest income, earnings at risk, regulatory capital,
capital requirement, capital reserve, RWA, risk weight, Basel, limit breaches,
etc.) as ANSWERABLE, as long as the underlying raw loan/borrower data needed to
compute them is present in the schema below (it is). Do NOT mark these
unanswerable just because they are not literal column names -- they are
computed downstream by models and tools, not retrieved directly via SQL.

In particular, "regulatory capital", "capital requirement", "RWA" and "Basel"
questions ARE answerable. The schema does not need a regulatory-capital column:
the Basel formula is implemented in Python and needs only outstanding_balance
plus the modelled PD and LGD, all of which are available.

The list of downstream metrics above is CLOSED -- those are the only metrics
computed after retrieval. Risk vocabulary alone does NOT make a question
answerable: the raw data the metric is built from must still be present in the
schema. A question needing a data dimension the schema does not have is
unanswerable no matter how it is phrased. Absent dimensions include currency
and FX rates, market or fair value prices, collateral and recovery records,
loan status / default / charge-off / delinquency outcomes, deposit account
balances, employee or loan-officer data, credit-bureau scores other than the
FICO range stored, and macroeconomic series. So "what is the FX exposure of the
portfolio" is FALSE (no currency data) even though "exposure" is risk
vocabulary, while "what is the expected loss on the portfolio" is TRUE.

Note also that this is a loan portfolio, not a full bank balance sheet. There is
no cash, no securities, no high-quality liquid assets, no equity or capital
account and no collateral valuation. So bank-level ratios that need those --
liquidity coverage ratio (LCR), net stable funding ratio (NSFR), CET1 or
leverage ratio, collateral coverage, loan-to-value -- are FALSE. Do not treat
`tot_coll_amt` (a credit-bureau collections amount owed) as collateral value.
The one exception is the loan-to-deposit ratio, which IS computed downstream
from the loan book plus the documented deposit assumption.
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
   metric (expected loss, PD, LGD, risk tier, risk profile, credit quality,
   HHI, concentration, repricing gap, interest rate risk, net interest income,
   regulatory capital, capital requirement, RWA, risk weight, Basel) rather
   than a raw column value -- see DOWNSTREAM COMPUTED METRICS above.

9. Additionally, determine whether answering this question requires computing
   risk metrics on top of the retrieved data, or whether it's answerable
   directly from the raw rows themselves (a count, a list, a simple
   lookup/filter, an average of a stored column).

   Set requires_risk_analysis=true whenever the question asks about risk,
   creditworthiness or capital in ANY wording -- not only when it names a
   metric exactly. Trigger words and phrases include:
   risk, risky, riskiness, risk profile, risk analysis, risk assessment,
   risk breakdown, credit quality, creditworthiness, how safe, how exposed,
   expected loss, loss estimate, PD, probability of default, default
   likelihood, LGD, loss given default, exposure at default, EAD, risk tier,
   concentration, HHI, diversification, repricing, interest rate risk,
   earnings at risk, net interest income, regulatory capital, capital
   requirement, capital reserve, RWA, risk-weighted assets, risk weight,
   Basel, compliance, limit breach.

   "Show/give me the risk profile of X", "how risky is X", "what is the credit
   quality of X" and "what is the regulatory capital for X" are all
   requires_risk_analysis=true. Grade and sub_grade are stored columns, but a
   question about the RISK of a graded population still needs the models -- do
   NOT answer it from the letter grade alone.

   Set requires_risk_analysis=false only when the question is satisfied by the
   rows themselves with no risk metric involved.

   Examples:
   "How many loans have sub_grade B3?" -> requires_risk_analysis: false
   "List loans in California" -> requires_risk_analysis: false
   "What is the average loan amount by purpose?" -> requires_risk_analysis: false
   "Which state has the most loans?" -> requires_risk_analysis: false
   "What is our expected loss for California loans?" -> requires_risk_analysis: true
   "What is our concentration risk by sector?" -> requires_risk_analysis: true
   "Show the risk profile of grade D and E loans issued in 2017." -> requires_risk_analysis: true
   "What is the credit quality of 60-month loans?" -> requires_risk_analysis: true
   "What is the regulatory capital requirement for loans under $10,000?" -> requires_risk_analysis: true
   "How risky is our Texas book?" -> requires_risk_analysis: true

10. A metric is answerable only if it is EITHER computable by SQL from the
    columns listed in the schema, OR one of the DOWNSTREAM COMPUTED METRICS
    named above. "Computed downstream" is not a general escape hatch -- the
    downstream list is fixed and no other metric is implemented. Holding SOME
    of a ratio's inputs is not enough: if its numerator or denominator does not
    exist in the schema, mark it false. An outstanding balance alone does not
    make every ratio with a balance in it answerable.

Examples:

Question: "What is the average loan amount in California?"
Schema contains loan_amnt and addr_state.
-> true

Question: "What is the regulatory capital requirement for loans under $10,000?"
Schema contains loan_amnt and outstanding_balance; PD and LGD are modelled
downstream and the Basel IRB formula is implemented in Python.
-> true, requires_risk_analysis: true

Question: "Show the risk profile of grade D and E loans issued in 2017."
Schema contains grade and issue_date; the risk metrics are computed downstream.
-> true, requires_risk_analysis: true

Question: "What percentage of loans have a FICO score above 700?"
Schema contains fico_range_low and fico_range_high.
-> true

Question: "What was the loan exposure in 2025?"
Schema contains loan amount and issue_date, but this portfolio's issue dates do
not reach 2025.
-> false

Question: "Which loans were charged off last year?"
Schema contains loan information but no loan status or default outcome -- the
Notes state no default/payoff outcome is stored.
-> false

Question: "How many loans are in Wyoming?"
Schema contains addr_state.
-> true
(Even if the database contains zero Wyoming loans.)

Question: "What is the current market price of our loans?"
Schema contains loan information but no market-price information.
-> false

Question: "What is the FX exposure of the portfolio?"
Schema contains outstanding balances but no currency field, so there is no way
to know which exposures are foreign-currency. "Exposure" here is not exposure
at default.
-> false

Question: "What is the collateral coverage ratio of the portfolio?"
Schema contains loan and balance amounts, but no collateral valuation, and
collateral coverage is not one of the downstream computed metrics. The
denominator does not exist.
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
