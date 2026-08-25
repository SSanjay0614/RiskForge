from pydantic import BaseModel, Field
from typing import List


class BucketGap(BaseModel):

    bucket_label: str = ""

    rate_sensitive_assets: float = 0.0

    rate_sensitive_liabilities: float = 0.0

    gap: float = 0.0


class RepricingGapResult(BaseModel):

    buckets: List[BucketGap] = Field(default_factory=list)

    total_rate_sensitive_assets: float = 0.0

    total_rate_sensitive_liabilities: float = 0.0

    net_gap: float = 0.0

    # Liability figures are a documented synthetic assumption, not observed
    # data -- see project README. Flagged on the result so downstream
    # consumers (e.g. the Compliance Agent) can label output accordingly.
    liabilities_are_synthetic: bool = True
