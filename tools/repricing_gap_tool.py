import pandas as pd

from tools.base_tool import BaseTool

from dmodels.repricing_gap_result import RepricingGapResult, BucketGap, RateShock


TERM_CODE_TO_MONTHS = {0: 36, 1: 60}  # matches the PD/LGD notebooks' term encoding


REPRICING_BUCKETS = [
    ("0-3mo", 0, 3),
    ("3-12mo", 3, 12),
    ("1-3yr", 12, 36),
    ("3yr+", 36, None),
]

# Synthetic liability structure -- Lending Club is a lending platform, not a
# bank balance sheet, so no deposit data exists to draw on. Distributed
# across buckets in a shape roughly typical of a retail deposit book
# (weighted toward short-term repricing, since most deposits are demand/
# short-term accounts). Documented assumption, not observed data -- see
# project README.
DEFAULT_LIABILITY_WEIGHTS = {
    "0-3mo": 0.55,
    "3-12mo": 0.25,
    "1-3yr": 0.15,
    "3yr+": 0.05,
}

# Deposit book size as a multiple of the loan book. Deposits fund the loans
# plus the liquid assets a bank has to carry alongside them, so a retail bank
# normally holds slightly more deposits than loans; 1.05 puts the resulting
# loan-to-deposit ratio at 0.95, inside the internal 1.1 ceiling and in the
# range a real consumer bank runs. Assumption, like the weights above.
DEFAULT_DEPOSIT_FUNDING_RATIO = 1.05

# Share of the interest earned on the loan book that is passed through to
# depositors as interest paid. At this portfolio's ~12.8% weighted-average
# contractual rate, 5% pass-through implies a ~0.64% deposit rate, which is
# what US retail savings accounts actually paid over the 2013-2018 issue
# window this data covers. Assumption.
DEFAULT_DEPOSIT_RATE_PASS_THROUGH = 0.05

# Fraction of the next 12 months for which a balance in each bucket carries
# the shocked rate, on the standard midpoint convention: a balance repricing
# on average at the bucket's midpoint is exposed for the rest of the year.
# Anything repricing beyond 12 months cannot affect a 12-month earnings
# measure at all, hence the zeros.
SHOCK_EXPOSURE_WEIGHTS_12M = {
    "0-3mo": 0.875,   # midpoint 1.5mo -> 10.5 of 12 months exposed
    "3-12mo": 0.375,  # midpoint 7.5mo -> 4.5 of 12 months exposed
    "1-3yr": 0.0,
    "3yr+": 0.0,
}

# Parallel shifts in the rate curve, in basis points. +/-200bp is the shock
# size supervisory rate-risk guidance conventionally starts from.
RATE_SHOCKS_BPS = (-200, -100, 100, 200)


class RepricingGapTool(BaseTool):
    """
    Computes interest rate risk via repricing gap analysis: for each time
    bucket, Gap = Rate-Sensitive Assets - Rate-Sensitive Liabilities.

    Assets are bucketed by months to the loan's next repricing event --
    approximated here as months remaining on fixed-rate term loans (a fixed
    loan "reprices" only at maturity/payoff).

    The gap table alone says which side of the balance sheet reprices first
    but not what that is worth, so the result also carries an earnings view:
    interest earned on the loans (from their real contractual int_rate),
    interest paid to depositors (a documented pass-through assumption), the
    resulting net interest income and margin, and the 12-month change in net
    interest income under parallel rate shocks. Every assumed input is
    returned alongside the figures it produced, so no consumer has to guess
    which numbers are observed and which are assumed.
    """

    def __init__(
        self,
        liability_weights: dict = None,
        total_liabilities: float = None,
        as_of_date: pd.Timestamp = None,
        deposit_funding_ratio: float = DEFAULT_DEPOSIT_FUNDING_RATIO,
        deposit_rate_pass_through: float = DEFAULT_DEPOSIT_RATE_PASS_THROUGH,
    ):

        super().__init__("Repricing Gap Tool")

        self.liability_weights = liability_weights or DEFAULT_LIABILITY_WEIGHTS
        self.total_liabilities = total_liabilities
        # Left as None deliberately -- resolved per-input from the portfolio's
        # own latest issue date. See _resolve_as_of_date.
        self.as_of_date = as_of_date
        self.deposit_funding_ratio = deposit_funding_ratio
        self.deposit_rate_pass_through = deposit_rate_pass_through

    def _assign_bucket(self, months_remaining: float) -> str:

        if pd.isna(months_remaining):
            # Unresolvable term or issue date. Treated as rolling off now, which
            # is the conservative near-term assumption; relying on comparison
            # fall-through instead silently parked these in the final bucket.
            months_remaining = 0.0

        for label, lower, upper in REPRICING_BUCKETS:
            if upper is None or months_remaining < upper:
                if months_remaining >= lower:
                    return label

        return REPRICING_BUCKETS[-1][0]

    def _resolve_as_of_date(self, loans: pd.DataFrame) -> pd.Timestamp:
        """The portfolio's own reporting date, not today's date.

        This is a 2013-2018 loan book. Measured against the wall clock every
        loan has long since matured, months_remaining collapses to zero for all
        of them, and the gap table becomes a single bar -- an artefact of when
        the report is run rather than a property of the portfolio. The latest
        issue date in the data is the date the snapshot was taken, so that is
        the correct as-of date for a repricing view.
        """

        if self.as_of_date is not None:
            return self.as_of_date

        if "issue_date" in loans.columns:
            latest = pd.to_datetime(loans["issue_date"], errors="coerce").max()
            if pd.notna(latest):
                return latest

        return pd.Timestamp.now()

    def _resolve_months_remaining(self, loans: pd.DataFrame) -> pd.Series:
        """
        Supports three input shapes, checked in order:
          1. A precomputed 'months_remaining' column, used as-is.
          2. 'term_months' + 'issue_date' -- months remaining is derived.
          3. 'term' + 'issue_date', where 'term' is either raw months (36/60,
             as the database stores it) or the 0/1 encoding the PD/LGD
             notebooks use.
        Raises with a clear message if none of these are present, rather than
        failing on a missing-column KeyError deeper in the pipeline.
        """

        if "months_remaining" in loans.columns:
            return loans["months_remaining"]

        if "issue_date" not in loans.columns:
            raise ValueError(
                "loans needs either a 'months_remaining' column, or "
                "'issue_date' plus one of 'term_months' / 'term' to derive it"
            )

        issue_date = pd.to_datetime(loans["issue_date"], errors="coerce")
        months_elapsed = (self._resolve_as_of_date(loans) - issue_date) / pd.Timedelta(days=30.44)

        if "term_months" in loans.columns:
            term_months = pd.to_numeric(loans["term_months"], errors="coerce")
        elif "term" in loans.columns:
            # Accept both storage forms. Mapping unconditionally through
            # TERM_CODE_TO_MONTHS turned the database's raw 36/60 into NaN,
            # which pushed every loan into the last bucket unnoticed.
            raw_term = pd.to_numeric(loans["term"], errors="coerce")
            term_months = raw_term.map(TERM_CODE_TO_MONTHS).where(
                raw_term.isin(list(TERM_CODE_TO_MONTHS)), raw_term
            )
        else:
            raise ValueError(
                "loans needs 'term_months' (raw) or 'term' (0/1 encoded) "
                "alongside 'issue_date' to derive months_remaining"
            )

        return (term_months - months_elapsed).clip(lower=0)

    def _earnings_view(self, loans: pd.DataFrame, total_rsa: float, deposit_base: float) -> dict:
        """Interest earned, interest paid, and net interest income.

        Returns an empty dict when int_rate is absent, rather than substituting
        an assumed yield -- a made-up asset yield would make every figure below
        an assumption, and the whole point of this block is that the asset side
        is real.
        """

        if "int_rate" not in loans.columns or total_rsa <= 0:
            return {}

        # int_rate is stored as a percentage (12.77), not a fraction.
        rate = pd.to_numeric(loans["int_rate"], errors="coerce") / 100.0
        exposure = pd.to_numeric(loans["exposure_at_default"], errors="coerce")
        usable = rate.notna() & exposure.notna()
        if not usable.any():
            return {}

        interest_income = float((exposure[usable] * rate[usable]).sum())
        portfolio_yield = interest_income / total_rsa
        deposit_rate = portfolio_yield * self.deposit_rate_pass_through
        interest_expense = deposit_base * deposit_rate

        return {
            "portfolio_yield": portfolio_yield,
            "deposit_rate": deposit_rate,
            "interest_income_annual": interest_income,
            "interest_expense_annual": interest_expense,
            "net_interest_income_annual": interest_income - interest_expense,
            "net_interest_margin": (interest_income - interest_expense) / total_rsa,
        }

    def _rate_shocks(self, buckets: list, net_interest_income: float) -> list:
        """12-month earnings-at-risk under parallel shifts in the curve.

        dNII = sum over buckets of (gap x shock x months-exposed-in-year). A
        negative gap in the near buckets means liabilities reprice first, so a
        rate rise costs money before the loan book catches up.
        """

        shocks = []
        for bps in RATE_SHOCKS_BPS:
            shift = bps / 10000.0
            change = sum(
                bucket.gap * shift * SHOCK_EXPOSURE_WEIGHTS_12M.get(bucket.bucket_label, 0.0)
                for bucket in buckets
            )
            shocks.append(RateShock(
                shock_bps=bps,
                net_interest_income_change=change,
                net_interest_income_after=net_interest_income + change,
                pct_change=(change / net_interest_income) if net_interest_income else 0.0,
            ))
        return shocks

    def run(self, loans: pd.DataFrame) -> RepricingGapResult:

        if "exposure_at_default" not in loans.columns:
            raise ValueError("loans must include an 'exposure_at_default' column")

        loans = loans.copy()
        as_of_date = self._resolve_as_of_date(loans)
        loans["months_remaining"] = self._resolve_months_remaining(loans)
        loans["bucket"] = loans["months_remaining"].apply(self._assign_bucket)

        rsa_by_bucket = loans.groupby("bucket")["exposure_at_default"].sum()
        total_rsa = float(rsa_by_bucket.sum())

        # Deposits are sized off total assets unless an explicit figure is
        # supplied -- see DEFAULT_DEPOSIT_FUNDING_RATIO for why it is not 1.0.
        total_liabilities = (
            self.total_liabilities
            if self.total_liabilities is not None
            else total_rsa * self.deposit_funding_ratio
        )

        buckets = []
        running_gap = 0.0
        for label, _, _ in REPRICING_BUCKETS:

            rsa = float(rsa_by_bucket.get(label, 0.0))
            rsl = float(total_liabilities * self.liability_weights.get(label, 0.0))
            running_gap += rsa - rsl

            buckets.append(
                BucketGap(
                    bucket_label=label,
                    rate_sensitive_assets=rsa,
                    rate_sensitive_liabilities=rsl,
                    gap=rsa - rsl,
                    cumulative_gap=running_gap,
                )
            )

        total_rsl = sum(b.rate_sensitive_liabilities for b in buckets)

        earnings = self._earnings_view(loans, total_rsa, total_rsl)
        shocks = self._rate_shocks(buckets, earnings.get("net_interest_income_annual", 0.0))

        # Liability-sensitive is judged on what reprices inside a year, not on
        # the total gap: the total is dominated by long-dated loan balances that
        # cannot affect the next 12 months of earnings either way.
        gap_within_12m = sum(
            bucket.gap for bucket in buckets if bucket.bucket_label in ("0-3mo", "3-12mo")
        )

        return RepricingGapResult(
            buckets=buckets,
            as_of_date=as_of_date.strftime("%Y-%m-%d"),
            total_rate_sensitive_assets=total_rsa,
            total_rate_sensitive_liabilities=total_rsl,
            net_gap=total_rsa - total_rsl,
            deposit_funding_ratio=self.deposit_funding_ratio,
            deposit_rate_pass_through=self.deposit_rate_pass_through,
            loan_to_deposit_ratio=(total_rsa / total_rsl) if total_rsl else 0.0,
            rate_shocks=shocks if earnings else [],
            earnings_at_risk_12m=min(
                [s.net_interest_income_change for s in shocks] + [0.0]
            ) if earnings else 0.0,
            is_liability_sensitive=gap_within_12m < 0,
            earnings_view_available=bool(earnings),
            **earnings,
        )
