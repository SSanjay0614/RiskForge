"""
The schema as the LLM sees it, in PostgreSQL terms.

Database/schema_description.py is the SQLite original and stays as it is -- the
local app still runs against credit_risk.db. This is the same description with
the three differences that matter once the target is RDS PostgreSQL:

  * **Types.** SQLite's REAL is PostgreSQL's DOUBLE PRECISION. Showing a model
    REAL invites `CAST(x AS REAL)`, which is valid PostgreSQL but silently means
    float4 -- 6 significant digits, on a column holding loan balances.
  * **Case.** Database/migration/01_schema.sql creates `loans`, `borrowers` and
    `risk_limits` unquoted, so PostgreSQL folded them to lowercase. Unquoted
    `FROM Loans` still resolves (it folds too), but a model shown `Loans` may
    write `FROM "Loans"`, and a *quoted* identifier is case-sensitive: that
    query fails with `relation "Loans" does not exist`. Naming them in
    lowercase here, plus the no-quoting rule in prompts.py, removes the trap.
  * **Dates are TEXT, deliberately.** The migration kept ISO date strings rather
    than casting to DATE, so `issue_date >= '2017-01-01'` works lexicographically
    but `EXTRACT(YEAR FROM issue_date)` is a type error. The Notes say so, so the
    model has to be told once rather than discover it through a failed query and
    a retry.

NOT NULL is recorded only where its absence is information the model needs --
`mths_since_last_delinq IS NULL` means "never delinquent", which is a filter a
question can legitimately ask for.
"""

SCHEMA_DESCRIPTION = """
loans (
    loan_id             TEXT PRIMARY KEY,
    loan_amnt           DOUBLE PRECISION,   -- original loan amount
    term                INTEGER,            -- months: 36 or 60
    int_rate            DOUBLE PRECISION,   -- interest rate, percent
    installment         DOUBLE PRECISION,   -- monthly payment amount
    grade               TEXT,               -- Lending Club letter grade A-G
    sub_grade           TEXT,               -- e.g. 'B3'
    purpose             TEXT,               -- e.g. 'debt_consolidation', 'credit_card'
    issue_date          TEXT,               -- ISO date string 'YYYY-MM-DD', loan origination
    outstanding_balance DOUBLE PRECISION,   -- current unpaid principal (exposure at default)
    on_payment_plan     INTEGER,            -- 0/1
    entered_hardship    INTEGER             -- 0/1
)

borrowers (
    loan_id                TEXT PRIMARY KEY REFERENCES loans(loan_id),
    annual_inc             DOUBLE PRECISION,
    emp_length             TEXT,             -- e.g. '2 years', '10+ years', '< 1 year'
    emp_title              TEXT,
    home_ownership         TEXT,             -- RENT, MORTGAGE, OWN, OTHER
    verification_status    TEXT,
    addr_state             TEXT,             -- 2-letter US state code
    dti                    DOUBLE PRECISION, -- debt-to-income ratio
    fico_range_low         INTEGER,
    fico_range_high        INTEGER,
    earliest_cr_line       TEXT,             -- ISO date string 'YYYY-MM-DD'
    open_acc               INTEGER,
    total_acc              INTEGER,
    revol_bal              DOUBLE PRECISION,
    revol_util             DOUBLE PRECISION,
    delinq_2yrs            INTEGER,
    acc_now_delinq         INTEGER,
    inq_last_6mths         INTEGER,
    mths_since_last_delinq INTEGER,          -- NULL means never delinquent
    mths_since_last_record INTEGER,          -- NULL means no public record
    num_tl_90g_dpd_24m     INTEGER,
    tot_coll_amt           DOUBLE PRECISION,
    tot_cur_bal            DOUBLE PRECISION,
    mo_sin_old_rev_tl_op   INTEGER,
    pct_tl_nvr_dlq         DOUBLE PRECISION,
    pub_rec                INTEGER,
    mort_acc               INTEGER,
    pub_rec_bankruptcies   INTEGER
)

risk_limits (
    limit_id    INTEGER PRIMARY KEY,
    metric_name TEXT,
    threshold   DOUBLE PRECISION,
    source      TEXT,
    description TEXT
)

Notes:
- The database is PostgreSQL.
- loans and borrowers are joined 1:1 on loan_id.
- All table and column names are lowercase. Write them unquoted.
- issue_date and earliest_cr_line are TEXT holding ISO 'YYYY-MM-DD' strings, not
  DATE columns. String comparison therefore orders them correctly
  (issue_date >= '2017-01-01' is valid), but a date function applied to them
  needs an explicit cast (issue_date::date).
- This is a portfolio of loans that are ACTIVE and UNRESOLVED (no default/payoff
  outcome is known or stored) -- questions about historical default rates or
  which specific loans defaulted cannot be answered from this schema.
- All data is read-only.
"""
