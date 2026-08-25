from pydantic import BaseModel, ConfigDict, Field
from typing import Dict, Optional
import pandas as pd


class ExpectedLossResult(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    loan_count: int = 0

    total_exposure: float = 0.0

    total_expected_loss: float = 0.0

    expected_loss_rate: float = 0.0

    exposure_weighted_avg_pd: float = 0.0

    exposure_weighted_avg_lgd: float = 0.0

    risk_tier_distribution: Dict[str, float] = Field(default_factory=dict)

    # Per-loan predicted_pd/predicted_lgd/exposure_at_default -- exposed so
    # downstream consumers (e.g. RegulatoryCapitalTool via ComplianceAgent)
    # can reuse these without re-loading and re-scoring the PD/LGD models.
    scored_df: Optional[pd.DataFrame] = None