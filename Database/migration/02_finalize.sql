-- Phase 6, step 3 of 3: run after the COPY loads.
--
-- Indexes are deliberately built after the data lands rather than declared in
-- 01_schema.sql. Building an index once over a finished table is a single sort;
-- maintaining it across 878,317 inserts is per-row work on every one of them.
-- On db.t4g.micro (2 burstable vCPU, 1 GiB RAM) that difference is the
-- difference between a load that finishes and one that eats its CPU credits.

BEGIN;

-- The four the SQLite schema had. These are the columns the text-to-SQL layer
-- filters on most: state, purpose, and sub-grade slices of the book, plus term
-- for the 36-vs-60-month split.
CREATE INDEX idx_borrowers_addr_state ON borrowers(addr_state);
CREATE INDEX idx_loans_purpose        ON loans(purpose);
CREATE INDEX idx_loans_sub_grade      ON loans(sub_grade);
CREATE INDEX idx_loans_term           ON loans(term);

-- Two the SQLite schema lacked, added because the risk tools query exactly
-- these: grade drives the Basel IRB capital and expected-loss breakdowns, and
-- issue_date drives the vintage and repricing-gap views.
CREATE INDEX idx_loans_grade      ON loans(grade);
CREATE INDEX idx_loans_issue_date ON loans(issue_date);

-- No index is added for the Loans/Borrowers join: both sides join on their own
-- primary key, so the PK indexes already serve it.

-- Advance the identity sequence past the five loaded limit_id values, otherwise
-- the next insert would collide with limit_id = 1.
SELECT setval(
    pg_get_serial_sequence('risk_limits', 'limit_id'),
    (SELECT COALESCE(MAX(limit_id), 1) FROM risk_limits)
);

COMMIT;

-- The planner has no statistics for a freshly loaded table until it is analysed,
-- and until then it will pick plans as if these tables were tiny. Autovacuum
-- would get here eventually; on a one-shot bulk load it is worth not waiting,
-- because the first queries after a migration are the ones being watched.
VACUUM ANALYZE loans;
VACUUM ANALYZE borrowers;
VACUUM ANALYZE risk_limits;
