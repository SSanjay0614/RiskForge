import sqlite3
from typing import List

from tools.base_tool import BaseTool

from dmodels.risk_limit import RiskLimit

from config import DB_PATH


class RiskLimitsTool(BaseTool):
    """
    Reads the Risk_Limits table -- both internal policy thresholds
    (source='internal') and Basel-sourced regulatory floors
    (source='basel_iii') -- as the single source of truth the Compliance
    Agent checks computed metrics against. Read-only, same pattern as
    SQLExecutorTool.
    """

    def __init__(self, db_path: str = DB_PATH):
        super().__init__("Risk Limits Tool")
        self.db_path = db_path

    def run(self) -> List[RiskLimit]:

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT metric_name, threshold, source, description FROM Risk_Limits"
            ).fetchall()
        finally:
            conn.close()

        return [
            RiskLimit(
                metric_name=r[0], threshold=r[1],
                source=r[2] or "", description=r[3] or "",
            )
            for r in rows
        ]
