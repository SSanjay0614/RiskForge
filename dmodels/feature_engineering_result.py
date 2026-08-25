from pydantic import BaseModel, ConfigDict, Field
from typing import Dict
import pandas as pd


class FeatureEngineeringResult(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    engineered_df: pd.DataFrame

    input_row_count: int = 0

    output_row_count: int = 0

    rows_dropped: int = 0

    # e.g. {'non_positive_dti': 3, 'missing_credit_report_cluster': 12}
    dropped_reason_counts: Dict[str, int] = Field(default_factory=dict)
