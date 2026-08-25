from pydantic import BaseModel, ConfigDict
from typing import List, Optional
import pandas as pd


class SQLExecutionResult(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool = False

    rows_df: Optional[pd.DataFrame] = None

    row_count: int = 0

    columns: List[str] = []

    error: Optional[str] = None

    # True if the query returned more rows than max_rows and was capped --
    # downstream consumers (and the evaluator) should know results are
    # partial, not silently treat them as the full answer.
    truncated: bool = False
