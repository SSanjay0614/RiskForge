from pydantic import BaseModel


class RegulatoryCapitalResult(BaseModel):
    """
    Basel III IRB "Other Retail" regulatory capital calculation, per
    CRE31.16 -- correlation R, capital requirement K, and risk-weighted
    assets, computed per-exposure and aggregated across the input population.
    """

    loan_count: int = 0

    total_ead: float = 0.0

    total_rwa: float = 0.0

    # Portfolio-level capital requirement in dollar terms (8% of RWA is the
    # Basel minimum capital ratio -- included for reference, not enforced
    # here as a rule; see project README on scope).
    total_capital_requirement_8pct: float = 0.0

    # Exposure-weighted averages, for reporting -- not a plain mean, since a
    # $200k exposure shouldn't count the same as a $2k one.
    exposure_weighted_avg_correlation: float = 0.0

    exposure_weighted_avg_k: float = 0.0

    avg_risk_weight_pct: float = 0.0
