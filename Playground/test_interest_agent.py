"""
Tests the Interest Rate & Concentration Agent against REAL rows pulled from
credit_risk.db -- no LLM involved. Hardcoded SQL run directly via
SQLExecutorTool, bypassing TextToSQLTool.
"""

from memory.state import RiskGraphState

from tools.sql_executor_tool import SQLExecutorTool

from agents.interest_rate_concentration_agent import InterestRateConcentrationAgent


executor = SQLExecutorTool()
agent = InterestRateConcentrationAgent()


# Broader pull than the credit risk test -- concentration metrics need a
# meaningful spread across purpose/addr_state to be informative.
sql_query = """
SELECT *
FROM Loans
JOIN Borrowers USING(loan_id)
LIMIT 5000
"""

exec_result = executor.run(sql_query)
print(f"Pulled {exec_result.row_count} real rows from credit_risk.db")

if exec_result.row_count == 0:
    print("No rows returned -- check the database is seeded.")
else:
    rows_df = exec_result.rows_df.copy()
    rows_df["exposure_at_default"] = rows_df["outstanding_balance"]

    state = RiskGraphState(query="interest rate & concentration agent isolated test")
    state.rows_df = rows_df

    updates = agent.run(state)
    rate_metrics = updates.get("rate_metrics", {})

    print()
    print("-" * 60)
    print("REPRICING GAP")
    gap = rate_metrics.get("repricing_gap")
    if gap:
        for bucket in gap["buckets"]:
            print(f"  {bucket['bucket_label']:>8} | Gap: {bucket['gap']:>14,.2f}")
        print(f"  Net gap: {gap['net_gap']:,.2f}")
    else:
        print("  Skipped -- check issue_date/term columns are present")

    print()
    print("-" * 60)
    print("CONCENTRATION BY PURPOSE (sector)")
    purpose_conc = rate_metrics.get("concentration_by_purpose")
    if purpose_conc:
        print(f"  HHI (0-10000): {purpose_conc['hhi_score_10000_scale']:.1f}")
        print(f"  Level: {purpose_conc['diversification_level']}")
        for segment, share in sorted(purpose_conc["segment_shares"].items(), key=lambda x: -x[1]):
            print(f"    {segment:>25}: {share:.4f}")
    else:
        print("  Skipped")

    print()
    print("-" * 60)
    print("CONCENTRATION BY REGION (addr_state)")
    region_conc = rate_metrics.get("concentration_by_region")
    if region_conc:
        print(f"  HHI (0-10000): {region_conc['hhi_score_10000_scale']:.1f}")
        print(f"  Level: {region_conc['diversification_level']}")
    else:
        print("  Skipped")
