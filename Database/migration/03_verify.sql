-- Phase 6 verification: a fingerprint of the loaded data, printed as label/value
-- pairs so it can be compared line by line against the same figures taken from
-- the SQLite source before the migration ran.
--
-- Row counts alone are not evidence. A load can produce the right number of
-- rows with columns shifted by one, a decimal truncated, or nulls turned into
-- zeros -- and every one of those would quietly change a risk number rather
-- than raise an error. So this checks three things counts cannot: that the
-- monetary and rate totals still add to the same value, that the null counts in
-- the seven nullable columns are unchanged (empty string and NULL are the same
-- token in CSV, and confusing them is the classic COPY bug), and that the
-- distinct-value cardinalities of the grouping columns still match.
--
-- Expected values, measured from Database/credit_risk.db on 2026-08-30:
--
--   loans_rows                    878317
--   borrowers_rows                878317
--   risk_limits_rows              5
--   sum_loan_amnt                 14002846250.00
--   sum_outstanding_balance       9125761309.86
--   sum_int_rate                  11214464.03
--   sum_term                      39098340
--   sum_annual_inc                70855256681.85
--   sum_revol_bal                 15194124092.00
--   sum_fico_range_low            617071405
--   sum_fico_range_high           620584923
--   distinct_grade                7
--   distinct_sub_grade            35
--   distinct_purpose              14
--   distinct_addr_state           50
--   min_issue_date                2013-10-01
--   max_issue_date                2018-12-01
--   null_emp_length               65470
--   null_emp_title                77824
--   null_dti                      1299
--   null_revol_util               867
--   null_mths_since_last_delinq   462640
--   null_mths_since_last_record   754780
--   null_pct_tl_nvr_dlq           1
--   empty_string_emp_title        0
--   orphan_borrowers              0
--
-- And from the final query, the California slice the tools exercise most:
--
--   ca_join_rows                  113008
--   ca_exposure                   1838158350.00
--
-- The exported CSVs were also re-parsed locally before upload and reproduced
-- every figure above, so a mismatch after the load points at COPY or the schema,
-- not at the export.

\pset pager off

SELECT 'loans_rows'                  AS check, COUNT(*)::text AS value FROM loans
UNION ALL SELECT 'borrowers_rows',       COUNT(*)::text FROM borrowers
UNION ALL SELECT 'risk_limits_rows',     COUNT(*)::text FROM risk_limits

UNION ALL SELECT 'sum_loan_amnt',           ROUND(SUM(loan_amnt)::numeric, 2)::text           FROM loans
UNION ALL SELECT 'sum_outstanding_balance', ROUND(SUM(outstanding_balance)::numeric, 2)::text FROM loans
UNION ALL SELECT 'sum_int_rate',            ROUND(SUM(int_rate)::numeric, 2)::text            FROM loans
UNION ALL SELECT 'sum_term',                SUM(term)::text                                   FROM loans
UNION ALL SELECT 'sum_annual_inc',          ROUND(SUM(annual_inc)::numeric, 2)::text          FROM borrowers
UNION ALL SELECT 'sum_revol_bal',           ROUND(SUM(revol_bal)::numeric, 2)::text           FROM borrowers
UNION ALL SELECT 'sum_fico_range_low',      SUM(fico_range_low)::text                         FROM borrowers
UNION ALL SELECT 'sum_fico_range_high',     SUM(fico_range_high)::text                        FROM borrowers

UNION ALL SELECT 'distinct_grade',      COUNT(DISTINCT grade)::text      FROM loans
UNION ALL SELECT 'distinct_sub_grade',  COUNT(DISTINCT sub_grade)::text  FROM loans
UNION ALL SELECT 'distinct_purpose',    COUNT(DISTINCT purpose)::text    FROM loans
UNION ALL SELECT 'distinct_addr_state', COUNT(DISTINCT addr_state)::text FROM borrowers

UNION ALL SELECT 'min_issue_date', MIN(issue_date) FROM loans
UNION ALL SELECT 'max_issue_date', MAX(issue_date) FROM loans

UNION ALL SELECT 'null_emp_length',             COUNT(*)::text FROM borrowers WHERE emp_length             IS NULL
UNION ALL SELECT 'null_emp_title',              COUNT(*)::text FROM borrowers WHERE emp_title              IS NULL
UNION ALL SELECT 'null_dti',                    COUNT(*)::text FROM borrowers WHERE dti                    IS NULL
UNION ALL SELECT 'null_revol_util',             COUNT(*)::text FROM borrowers WHERE revol_util             IS NULL
UNION ALL SELECT 'null_mths_since_last_delinq', COUNT(*)::text FROM borrowers WHERE mths_since_last_delinq IS NULL
UNION ALL SELECT 'null_mths_since_last_record', COUNT(*)::text FROM borrowers WHERE mths_since_last_record IS NULL
UNION ALL SELECT 'null_pct_tl_nvr_dlq',         COUNT(*)::text FROM borrowers WHERE pct_tl_nvr_dlq         IS NULL

-- CSV cannot distinguish an unquoted empty field from a quoted empty string
-- unless the writer is careful, so this is the assertion that it was: the source
-- has 77,824 NULL emp_titles and zero empty ones.
UNION ALL SELECT 'empty_string_emp_title', COUNT(*)::text FROM borrowers WHERE emp_title = ''

-- Belt and braces. The foreign key makes this impossible to insert, so a
-- non-zero here would mean the constraint is missing, not that data is bad.
UNION ALL SELECT 'orphan_borrowers', COUNT(*)::text
    FROM borrowers b LEFT JOIN loans l USING (loan_id) WHERE l.loan_id IS NULL
;

-- The join the retrieval layer actually issues, on the population the tools use
-- most. Confirms the join produces one row per loan rather than fanning out.
SELECT COUNT(*) AS ca_join_rows, ROUND(SUM(l.loan_amnt)::numeric, 2) AS ca_exposure
FROM loans l JOIN borrowers b USING (loan_id)
WHERE b.addr_state = 'CA';
