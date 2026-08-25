"""
Tests the Credit Risk Agent against REAL rows pulled from credit_risk.db --
no LLM involved (this agent makes no LLM calls), so the SQL here is a
hardcoded query run directly via SQLExecutorTool, bypassing TextToSQLTool.
Needs the real seeded database and the real trained PD/LGD model artifacts.
"""

from memory.state import RiskGraphState

from tools.sql_executor_tool import SQLExecutorTool

from agents.credit_risk_agent import CreditRiskAgent


executor = SQLExecutorTool()
agent = CreditRiskAgent()


# Real row-level query against the actual schema -- no aggregation, matching
# what the Data Agent's SQL is now constrained to produce.
sql_query = """
SELECT *
FROM Loans
JOIN Borrowers USING(loan_id)
WHERE Loans.sub_grade = 'B3'
LIMIT 500
"""

exec_result = executor.run(sql_query)
print(f"Pulled {exec_result.row_count} real rows from credit_risk.db")
print(f"Columns: {exec_result.columns}")

if exec_result.row_count == 0:
    print("No rows returned -- try a broader query (e.g. different sub_grade or no filter).")
else:
    # exposure_at_default alias normally added by DataAgent.execute_sql() --
    # replicated here since we're bypassing DataAgent for this isolated test.
    rows_df = exec_result.rows_df.copy()
    rows_df["exposure_at_default"] = rows_df["outstanding_balance"]

    state = RiskGraphState(query="credit risk agent isolated test")
    state.rows_df = rows_df

    updates = agent.run(state)

    print()
    print("-" * 60)
    print(f"credit_metrics: {updates.get('credit_metrics')}")
    print()
    engineered_df = updates.get("engineered_df")
    if engineered_df is not None:
        print(f"engineered_df shape: {engineered_df.shape}")
        print(f"engineered_df columns: {list(engineered_df.columns)}")
