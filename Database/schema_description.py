SCHEMA_DESCRIPTION = """
Loans (
    loan_id TEXT PRIMARY KEY,
    loan_amnt REAL,              -- original loan amount
    term INTEGER,                -- months: 36 or 60
    int_rate REAL,               -- interest rate, percent
    installment REAL,            -- monthly payment amount
    grade TEXT,                  -- Lending Club letter grade A-G
    sub_grade TEXT,              -- e.g. 'B3'
    purpose TEXT,                -- e.g. 'debt_consolidation', 'credit_card'
    issue_date TEXT,             -- ISO date, loan origination
    outstanding_balance REAL,    -- current unpaid principal (exposure at default)
    on_payment_plan INTEGER,     -- 0/1
    entered_hardship INTEGER     -- 0/1
)

Borrowers (
    loan_id TEXT PRIMARY KEY REFERENCES Loans(loan_id),
    annual_inc REAL,
    emp_length TEXT,             -- e.g. '2 years', '10+ years', '< 1 year'
    emp_title TEXT,
    home_ownership TEXT,         -- RENT, MORTGAGE, OWN, OTHER
    verification_status TEXT,
    addr_state TEXT,             -- 2-letter US state code
    dti REAL,                    -- debt-to-income ratio
    fico_range_low INTEGER,
    fico_range_high INTEGER,
    earliest_cr_line TEXT,       -- ISO date
    open_acc INTEGER,
    total_acc INTEGER,
    revol_bal REAL,
    revol_util REAL,
    delinq_2yrs INTEGER,
    acc_now_delinq INTEGER,
    inq_last_6mths INTEGER,
    mths_since_last_delinq INTEGER,   -- NULL means never delinquent
    mths_since_last_record INTEGER,   -- NULL means no public record
    num_tl_90g_dpd_24m INTEGER,
    tot_coll_amt REAL,
    tot_cur_bal REAL,
    mo_sin_old_rev_tl_op INTEGER,
    pct_tl_nvr_dlq REAL,
    pub_rec INTEGER,
    mort_acc INTEGER,
    pub_rec_bankruptcies INTEGER
)

Risk_Limits (
    limit_id INTEGER PRIMARY KEY,
    metric_name TEXT,
    threshold REAL,
    source TEXT,
    description TEXT
)

Notes:
- Loans and Borrowers are joined 1:1 on loan_id.
- This is a portfolio of loans that are ACTIVE and UNRESOLVED (no default/payoff
  outcome is known or stored) -- questions about historical default rates or
  which specific loans defaulted cannot be answered from this schema.
- All data is read-only.
"""
