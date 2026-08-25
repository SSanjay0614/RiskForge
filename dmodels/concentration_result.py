from pydantic import BaseModel, Field
from typing import Dict


class ConcentrationResult(BaseModel):

    segment_column: str = ""

    hhi_score: float = 0.0

    # HHI expressed on the conventional 0-10000 scale (DOJ merger-guideline
    # convention: shares as percentages, squared and summed) -- more widely
    # recognized than the raw 0-1 fractional-share score.
    hhi_score_10000_scale: float = 0.0

    diversification_level: str = ""

    segment_shares: Dict[str, float] = Field(default_factory=dict)
