import re
import sqlite3

import pandas as pd

from tools.base_tool import BaseTool

from dmodels.sql_execution_result import SQLExecutionResult

from config import DB_PATH


# Statement-type check is a fast pre-filter, not the real security boundary --
# the read-only connection below is what actually prevents writes, regardless
# of what the query text claims to be.
BLOCKED_KEYWORDS = re.compile(
    r"(?is)\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|DETACH|PRAGMA|VACUUM|REPLACE)\b"
)


class SQLExecutorTool(BaseTool):
    """
    Executes a SQL SELECT statement against the loan portfolio database,
    strictly read-only. Genuine enforcement comes from opening the SQLite
    connection in read-only mode (mode=ro) -- a malicious or malformed query
    physically cannot write to the database through this connection,
    regardless of what the query text says.

    Catches execution errors (bad syntax, unknown columns) and returns them
    in the result rather than raising, so a calling agent can feed the error
    back to TextToSQLTool as retry feedback instead of crashing.
    """

    def __init__(self, db_path: str = DB_PATH, max_rows: int = 2_000_000):
        # max_rows is a pathological-query safety net (e.g. an accidental
        # cross-join), NOT a normal-operation limiter -- it's set well above
        # this dataset's actual size (878K loans) so it never triggers on a
        # legitimate full-portfolio query. A low cap here would silently
        # corrupt aggregate results (e.g. "total portfolio EL") rather than
        # protect anything meaningful at this data scale.
        super().__init__("SQL Executor Tool")
        self.db_path = db_path
        self.max_rows = max_rows

    def run(self, sql_query: str) -> SQLExecutionResult:

        sql_query = sql_query.strip()

        if not re.match(r"(?is)^\s*SELECT\b", sql_query):
            return SQLExecutionResult(
                success=False, error="Only SELECT statements are permitted."
            )

        if BLOCKED_KEYWORDS.search(sql_query):
            return SQLExecutionResult(
                success=False, error="Query contains a disallowed keyword."
            )

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError as e:
            return SQLExecutionResult(success=False, error=f"Could not open database: {e}")

        try:
            rows_df = pd.read_sql_query(sql_query, conn)
        except (sqlite3.Error, pd.errors.DatabaseError) as e:
            return SQLExecutionResult(success=False, error=str(e))
        finally:
            conn.close()

        truncated = len(rows_df) > self.max_rows
        if truncated:
            rows_df = rows_df.head(self.max_rows)

        return SQLExecutionResult(
            success=True,
            rows_df=rows_df,
            row_count=len(rows_df),
            columns=list(rows_df.columns),
            truncated=truncated,
        )
