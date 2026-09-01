"""
The three prompts, ported from tools/ for PostgreSQL and for a hosted model.

GUARD_PROMPT and EVALUATOR_PROMPT are carried across verbatim -- neither says
anything SQLite-specific, and rewording a prompt already tuned against real
questions is how you lose behaviour you cannot get back. Only the one table name
the guard prompt mentions by name changed case. SQL_GENERATION_PROMPT is the one
that needed real work:

  * "READ-ONLY SQLite queries" and "Use valid SQLite syntax" named the wrong
    engine, which is how a model ends up emitting strftime() against a
    PostgreSQL server.
  * Rule 2's join is now `JOIN borrowers USING(loan_id)`, and rule 10 forbids
    quoting identifiers. Unquoted names fold to lowercase in PostgreSQL, so
    `FROM Loans` works -- but `FROM "Loans"` is case-sensitive and fails. A model
    shown capitalised table names sometimes quotes them.
  * Rule 14 listed SQLite's escape hatches (PRAGMA, ATTACH). The PostgreSQL ones
    are different, and `SELECT ... INTO` matters most: it is a write that starts
    with SELECT, which is exactly the case Deploy/lambda/test_functions.py proves
    the server refuses.
  * Rule 19 is new. The migration kept dates as ISO TEXT rather than casting to
    DATE, so string comparison is correct and EXTRACT is a type error.

The trailing "return only JSON" instructions are kept even though the API is
also given a response schema (see gemini.py). Belt and braces: the schema
constrains the response, the instruction survives a model that ignores schemas,
and the two agree.

Generated from the tools/ sources rather than retyped, so "verbatim" is a fact
rather than an intention.
"""
from schema_postgres import SCHEMA_DESCRIPTION  # noqa: F401  (re-exported)


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
- Compliance checks of the above against the thresholds in risk_limits

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


SQL_GENERATION_PROMPT = """You are a Text-to-SQL agent that generates READ-ONLY
PostgreSQL queries against the following database schema.

DATABASE SCHEMA:
{schema}

USER QUESTION:
"{query}"

{feedback_block}

Your job is ONLY to identify and retrieve the rows relevant to the user's
question. Aggregation, statistical calculations, and risk calculations are
performed downstream in Python.

Rules for the query:

1. Generate exactly ONE PostgreSQL SELECT statement.

2. Always SELECT every column from both loans and borrowers using a full join
   on loan_id, written as JOIN borrowers USING(loan_id) -- NOT
   "ON loans.loan_id = borrowers.loan_id", which duplicates the loan_id
   column in the result set. Never select only a subset of columns.

3. NEVER use aggregate functions such as COUNT, SUM, AVG, MIN, MAX, or similar
   functions.

4. NEVER use GROUP BY.

5. Do not perform aggregation, statistical calculations, or risk calculations
   in SQL. These operations happen downstream in Python.

6. Your only job is to retrieve the correct rows and apply the appropriate
   filtering conditions.

7. Only add WHERE conditions that are clearly implied by the user's question.
   Do not invent filters, assumptions, thresholds, categories, dates, or values.

8. If the question does not specify any filtering condition, return all rows
   without a WHERE clause.

9. Use ONLY tables, columns, and relationships that exist in the provided
   schema. Never invent a table, column, relationship, or value.

10. Use valid PostgreSQL syntax. Every table and column name is lowercase;
    write them unquoted. Never wrap an identifier in double quotes -- a
    quoted identifier is case-sensitive in PostgreSQL, so "Loans" raises
    `relation "Loans" does not exist` while unquoted Loans folds to loans
    and works.

11. If the question refers to a field or concept, use the corresponding field
    from the schema. Do not substitute an unrelated field merely because its
    name appears similar.

12. Preserve the meaning of the user's filters exactly. For example, if the
    user asks for California loans, filter for the California value in the
    appropriate state column.

13. If a previous attempt was rejected, use the evaluator feedback to correct
    the query. Fix the specific problem identified without unnecessarily
    changing parts of the query that were already correct.

14. Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE,
    GRANT, REVOKE, COPY, DO, CALL, SET, or SELECT ... INTO, or any other
    non-SELECT statement.

15. Do not generate multiple SQL statements or use semicolon-separated
    statements.

16. Do not include SQL comments.

17. Do not explain your reasoning.

18. Return ONLY the SQL query. Do not use markdown fences.

19. issue_date and earliest_cr_line are TEXT columns holding ISO
    'YYYY-MM-DD' strings, not DATE columns. Compare them as strings --
    loans issued in 2017 are
    issue_date >= '2017-01-01' AND issue_date < '2018-01-01'. Do not apply
    EXTRACT, DATE_PART or strftime to them without an explicit ::date cast.

The output must be a single SELECT statement that retrieves all columns from
loans and borrowers through their loan_id relationship and filters only the
rows relevant to the user's question.
"""


FEEDBACK_BLOCK_TEMPLATE = """
A previous SQL attempt was rejected by the SQL Evaluator.

Evaluator feedback:
"{feedback}"

Generate a corrected query that addresses the specific problem identified in
the feedback while still answering the original question and following all
rules above.
Do not blindly change unrelated parts of the query.
"""


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


# Zero rows is rejected without a model call: it essentially always means a
# filter was wrong, and a judgement call adds nothing to a fixed message.
# Carried across from SQLEvaluatorTool.run() unchanged.
EMPTY_RESULT_FEEDBACK = (
    "The query returned zero rows -- check filter conditions "
    "and column names against the schema."
)
