from pydantic import BaseModel


class RiskLimit(BaseModel):

    metric_name: str = ""

    threshold: float = 0.0

    source: str = ""  # 'internal' or 'basel_iii'

    description: str = ""
