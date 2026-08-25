-- Loan Portfolio Risk Copilot -- Database Schema
-- Three tables: Loans (loan-level facts), Borrowers (borrower-level facts,
-- 1:1 with Loans via loan_id -- Lending Club doesn't provide a true separate
-- borrower entity, so this is a normalized split rather than a real 1:many
-- relationship), and Risk_Limits (compliance thresholds).
--
-- No PD/LGD predictions are stored here. Those are computed live by
-- ExpectedLossTool against whatever subset a query returns, so risk numbers
-- never go stale relative to the trained models.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS Loans (
    loan_id             TEXT PRIMARY KEY,
    loan_amnt           REAL,
    term                INTEGER,
    int_rate            REAL,
    installment         REAL,
    grade               TEXT,
    sub_grade           TEXT,
    purpose             TEXT,
    issue_date          TEXT,
    outstanding_balance REAL,
    on_payment_plan     INTEGER,
    entered_hardship    INTEGER
);

CREATE TABLE IF NOT EXISTS Borrowers (
    loan_id               TEXT PRIMARY KEY REFERENCES Loans(loan_id),
    annual_inc            REAL,
    emp_length            TEXT,
    emp_title             TEXT,
    home_ownership        TEXT,
    verification_status   TEXT,
    addr_state            TEXT,
    dti                   REAL,
    fico_range_low        INTEGER,
    fico_range_high       INTEGER,
    earliest_cr_line      TEXT,
    open_acc              INTEGER,
    total_acc             INTEGER,
    revol_bal             REAL,
    revol_util            REAL,
    delinq_2yrs           INTEGER,
    acc_now_delinq        INTEGER,
    inq_last_6mths        INTEGER,
    mths_since_last_delinq   INTEGER,
    mths_since_last_record   INTEGER,
    num_tl_90g_dpd_24m       INTEGER,
    tot_coll_amt              REAL,
    tot_cur_bal                REAL,
    mo_sin_old_rev_tl_op        INTEGER,
    pct_tl_nvr_dlq                REAL,
    pub_rec                       INTEGER,
    mort_acc                      INTEGER,
    pub_rec_bankruptcies          INTEGER
);

CREATE TABLE IF NOT EXISTS Risk_Limits (
    limit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name  TEXT NOT NULL,
    threshold    REAL NOT NULL,
    source       TEXT,
    description  TEXT
);

CREATE INDEX IF NOT EXISTS idx_loans_sub_grade ON Loans(sub_grade);
CREATE INDEX IF NOT EXISTS idx_loans_purpose ON Loans(purpose);
CREATE INDEX IF NOT EXISTS idx_loans_term ON Loans(term);
CREATE INDEX IF NOT EXISTS idx_borrowers_addr_state ON Borrowers(addr_state);