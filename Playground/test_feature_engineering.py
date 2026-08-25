import pandas as pd
import numpy as np

from tools.feature_engineering_tool import FeatureEngineeringTool


tool = FeatureEngineeringTool()

# Synthetic sample shaped like a Loans JOIN Borrowers query result -- swap in
# a real SQLite query result once the Data Agent is wired up.
rng = np.random.default_rng(42)
n = 100

raw_df = pd.DataFrame({
    "loan_amnt": rng.uniform(2000, 35000, n),
    "term": rng.choice([36, 60], n),
    "int_rate": rng.uniform(5, 25, n),
    "installment": rng.uniform(100, 1200, n),
    "sub_grade": rng.choice(["A1", "B3", "C2", "D4", "E1"], n),
    "purpose": rng.choice(["debt_consolidation", "credit_card", "home_improvement"], n),
    "issue_date": pd.to_datetime("2017-06-01"),
    "outstanding_balance": rng.uniform(1000, 30000, n),
    "on_payment_plan": rng.choice([0, 1], n, p=[0.95, 0.05]),
    "entered_hardship": rng.choice([0, 1], n, p=[0.97, 0.03]),
    "annual_inc": rng.uniform(25000, 150000, n),
    "emp_length": rng.choice(["< 1 year", "2 years", "5 years", "10+ years"], n),
    "emp_title": rng.choice(["Teacher", "Manager", "Engineer", None], n),
    "home_ownership": rng.choice(["RENT", "MORTGAGE", "OWN"], n),
    "verification_status": rng.choice(["Verified", "Not Verified", "Source Verified"], n),
    "addr_state": rng.choice(["CA", "TX", "NY", "FL"], n),
    "dti": rng.uniform(1, 35, n),
    "fico_range_low": rng.integers(660, 800, n),
    "fico_range_high": rng.integers(660, 800, n),
    "earliest_cr_line": pd.to_datetime("2005-01-01"),
    "open_acc": rng.integers(2, 20, n),
    "total_acc": rng.integers(5, 40, n),
    "revol_bal": rng.uniform(0, 40000, n),
    "revol_util": rng.uniform(0, 100, n),
    "delinq_2yrs": rng.integers(0, 3, n),
    "acc_now_delinq": 0,
    "inq_last_6mths": rng.integers(0, 4, n),
    "mths_since_last_delinq": rng.choice([np.nan, 6, 18, 30], n),
    "mths_since_last_record": np.nan,
    "num_tl_90g_dpd_24m": 0,
    "tot_coll_amt": rng.uniform(0, 500, n),
    "tot_cur_bal": rng.uniform(5000, 100000, n),
    "mo_sin_old_rev_tl_op": rng.integers(24, 240, n),
    "pct_tl_nvr_dlq": rng.uniform(80, 100, n),
    "pub_rec": 0,
    "mort_acc": rng.integers(0, 3, n),
    "pub_rec_bankruptcies": 0,
})

result = tool.run(raw_df)

print("-" * 60)
print(f"Input rows:   {result.input_row_count}")
print(f"Output rows:  {result.output_row_count}")
print(f"Rows dropped: {result.rows_dropped}")
print(f"Dropped by reason: {result.dropped_reason_counts}")
print(f"Engineered shape: {result.engineered_df.shape}")
print(f"Columns: {list(result.engineered_df.columns)}")
