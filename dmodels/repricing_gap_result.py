from pydantic import BaseModel, Field
from typing import List


class BucketGap(BaseModel):

    bucket_label: str = ""

    rate_sensitive_assets: float = 0.0

    rate_sensitive_liabilities: float = 0.0

    gap: float = 0.0

    # Running total of `gap` across buckets in maturity order. The periodic gap
    # answers "what reprices in this window", the cumulative gap answers "what
    # is repriced by the end of it" -- the second is the one that drives an
    # earnings measure, so both are reported rather than left to the caller.
    cumulative_gap: float = 0.0


class RateShock(BaseModel):
    """12-month earnings-at-risk for one parallel shift in the rate curve."""

    shock_bps: int = 0

    # Change in the next 12 months of net interest income, in currency.
    net_interest_income_change: float = 0.0

    net_interest_income_after: float = 0.0

    # Change as a share of unshocked net interest income.
    pct_change: float = 0.0


class RepricingGapResult(BaseModel):

    buckets: List[BucketGap] = Field(default_factory=list)

    # Reporting date the buckets are measured from -- the portfolio's own latest
    # issue date, not the day the report was run. See RepricingGapTool.
    as_of_date: str = ""

    total_rate_sensitive_assets: float = 0.0

    total_rate_sensitive_liabilities: float = 0.0

    net_gap: float = 0.0

    # --- Funding and earnings view ---------------------------------------
    # A gap table on its own says which side reprices first but not what that
    # costs. These fields turn it into an earnings measure: the asset yield is
    # the portfolio's real weighted-average contractual rate, the deposit side
    # is the documented assumption below.

    # Weighted-average contractual loan rate, as a fraction (0.1277 = 12.77%).
    # Read from the portfolio's own int_rate -- not an assumption.
    portfolio_yield: float = 0.0

    # Deposit book size as a multiple of the loan book, and the share of the
    # asset yield passed through to depositors. Both are assumptions.
    deposit_funding_ratio: float = 0.0

    deposit_rate_pass_through: float = 0.0

    # portfolio_yield x deposit_rate_pass_through, as a fraction.
    deposit_rate: float = 0.0

    interest_income_annual: float = 0.0

    interest_expense_annual: float = 0.0

    net_interest_income_annual: float = 0.0

    # Net interest income over rate-sensitive assets, as a fraction.
    net_interest_margin: float = 0.0

    # Loans / deposits. Checked against the internal max_loan_to_deposit_ratio
    # limit by the Compliance Agent.
    loan_to_deposit_ratio: float = 0.0

    rate_shocks: List[RateShock] = Field(default_factory=list)

    # The worst 12-month net interest income change across the shocks above
    # (<= 0). The single number a treasury committee asks for.
    earnings_at_risk_12m: float = 0.0

    # True when more liabilities than assets reprice inside 12 months, i.e.
    # rising rates compress net interest income in the near term.
    is_liability_sensitive: bool = False

    # True when int_rate was absent from the input, so every field in the
    # earnings view above is zero and should not be displayed.
    earnings_view_available: bool = False

    # Liability figures are a documented synthetic assumption, not observed
    # data -- see project README. Flagged on the result so downstream
    # consumers (e.g. the Compliance Agent) can label output accordingly.
    liabilities_are_synthetic: bool = True
