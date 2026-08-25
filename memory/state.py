from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Dict
import pandas as pd

from dmodels.data_agent_result import DataAgentResult


class RiskGraphState(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ---------------------------------
    # Query & Data Retrieval (Data Agent)
    # ---------------------------------

    query: str = ""

    sql_query: str = ""

    rows_df: Optional[pd.DataFrame] = None

    row_count: int = 0

    truncated: bool = False

    retry_count: int = 0

    max_retries: int = 3

    evaluator_feedback: Optional[str] = None

    data_available: bool = True

    requires_risk_analysis: bool = True

    guard_reason: str = ""

    data_agent_result: Optional[DataAgentResult] = None


    # ---------------------------------
    # Credit Risk (Credit Risk Agent)
    # ---------------------------------

    engineered_df: Optional[pd.DataFrame] = None

    scored_df: Optional[pd.DataFrame] = None

    credit_metrics: Dict = Field(default_factory=dict)


    # ---------------------------------
    # Interest Rate & Concentration (Interest Rate & Concentration Agent)
    # ---------------------------------

    rate_metrics: Dict = Field(default_factory=dict)


    # ---------------------------------
    # Compliance (Compliance Agent)
    # ---------------------------------

    compliance_result: Dict = Field(default_factory=dict)


    # ---------------------------------
    # Output
    # ---------------------------------

    final_report: str = ""

    current_agent: str = ""