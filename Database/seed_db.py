import sqlite3
import re

import pandas as pd

from config import DB_PATH, RAW_CSV_PATH


LOAN_COLS_RAW_TO_SCHEMA = {
    "id": "loan_id",
    "loan_amnt": "loan_amnt",
    "term": "term",
    "int_rate": "int_rate",
    "installment": "installment",
    "grade": "grade",
    "sub_grade": "sub_grade",
    "purpose": "purpose",
    "issue_d": "issue_date",
    "out_prncp": "outstanding_balance",
}

BORROWER_COLS_RAW_TO_SCHEMA = {
    "id": "loan_id",
    "annual_inc": "annual_inc",
    "emp_length": "emp_length",
    "emp_title": "emp_title",
    "home_ownership": "home_ownership",
    "verification_status": "verification_status",
    "addr_state": "addr_state",
    "dti": "dti",
    "fico_range_low": "fico_range_low",
    "fico_range_high": "fico_range_high",
    "earliest_cr_line": "earliest_cr_line",
    "open_acc": "open_acc",
    "total_acc": "total_acc",
    "revol_bal": "revol_bal",
    "revol_util": "revol_util",
    "delinq_2yrs": "delinq_2yrs",
    "acc_now_delinq": "acc_now_delinq",
    "inq_last_6mths": "inq_last_6mths",
    "mths_since_last_delinq": "mths_since_last_delinq",
    "mths_since_last_record": "mths_since_last_record",
    "num_tl_90g_dpd_24m": "num_tl_90g_dpd_24m",
    "tot_coll_amt": "tot_coll_amt",
    "tot_cur_bal": "tot_cur_bal",
    "mo_sin_old_rev_tl_op": "mo_sin_old_rev_tl_op",
    "pct_tl_nvr_dlq": "pct_tl_nvr_dlq",
    "pub_rec": "pub_rec",
    "mort_acc": "mort_acc",
    "pub_rec_bankruptcies": "pub_rec_bankruptcies",
}

# Extra raw columns needed only as inputs to derive schema columns, not
# stored under their own name.
EXTRA_RAW_COLS = ["pymnt_plan", "hardship_flag", "loan_status"]

DEFAULT_RISK_LIMITS = [
    ("max_expected_loss_rate", 0.05, "internal", "Portfolio-level EL as a share of total exposure"),
    ("max_hhi_10000_scale", 2500.0, "internal", "DOJ-convention concentration threshold, highly concentrated above this"),
    ("max_loan_to_deposit_ratio", 1.10, "internal", "Whole-book liquidity indicator, illustrative"),
]


def _parse_term_months(term_str: str) -> int:
    match = re.search(r"\d+", str(term_str))
    return int(match.group()) if match else None


def load_and_transform() -> tuple[pd.DataFrame, pd.DataFrame]:

    usecols = (
        list(LOAN_COLS_RAW_TO_SCHEMA.keys())
        + [c for c in BORROWER_COLS_RAW_TO_SCHEMA.keys() if c not in LOAN_COLS_RAW_TO_SCHEMA]
        + EXTRA_RAW_COLS
    )
    usecols = list(dict.fromkeys(usecols))  # de-dupe while preserving order ('id' appears twice)

    print(f"Reading {RAW_CSV_PATH} ...")
    raw = pd.read_csv(RAW_CSV_PATH, low_memory=False, usecols=usecols)

    raw = raw[raw["loan_status"] == "Current"].copy()
    print(f"Current-status loans: {len(raw)}")

    raw["term"] = raw["term"].apply(_parse_term_months)
    raw["issue_d"] = pd.to_datetime(raw["issue_d"], format="%b-%Y", errors="coerce").dt.strftime("%Y-%m-01")
    raw["earliest_cr_line"] = pd.to_datetime(
        raw["earliest_cr_line"], format="%b-%Y", errors="coerce"
    ).dt.strftime("%Y-%m-01")

    raw["on_payment_plan"] = (raw["pymnt_plan"] == "y").astype(int)
    raw["entered_hardship"] = (raw["hardship_flag"] == "Y").astype(int)

    loans_df = raw.rename(columns=LOAN_COLS_RAW_TO_SCHEMA)[
        list(LOAN_COLS_RAW_TO_SCHEMA.values()) + ["on_payment_plan", "entered_hardship"]
    ]

    borrowers_df = raw.rename(columns=BORROWER_COLS_RAW_TO_SCHEMA)[
        list(BORROWER_COLS_RAW_TO_SCHEMA.values())
    ]

    return loans_df, borrowers_df


def seed_database():

    loans_df, borrowers_df = load_and_transform()

    conn = sqlite3.connect(DB_PATH)

    print("Writing Loans table ...")
    loans_df.to_sql("Loans", conn, if_exists="append", index=False, chunksize=10000)

    print("Writing Borrowers table ...")
    borrowers_df.to_sql("Borrowers", conn, if_exists="append", index=False, chunksize=10000)

    print("Seeding Risk_Limits ...")
    conn.executemany(
        "INSERT INTO Risk_Limits (metric_name, threshold, source, description) VALUES (?, ?, ?, ?)",
        DEFAULT_RISK_LIMITS,
    )

    conn.commit()

    loan_count = conn.execute("SELECT COUNT(*) FROM Loans").fetchone()[0]
    borrower_count = conn.execute("SELECT COUNT(*) FROM Borrowers").fetchone()[0]
    limit_count = conn.execute("SELECT COUNT(*) FROM Risk_Limits").fetchone()[0]

    print(f"Loans: {loan_count}, Borrowers: {borrower_count}, Risk_Limits: {limit_count}")

    conn.close()


if __name__ == "__main__":
    seed_database()
