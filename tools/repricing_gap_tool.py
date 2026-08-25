import pandas as pd

from tools.base_tool import BaseTool

from dmodels.repricing_gap_result import RepricingGapResult, BucketGap


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


class RepricingGapTool(BaseTool):
    """
    Computes interest rate risk via repricing gap analysis: for each time
    bucket, Gap = Rate-Sensitive Assets - Rate-Sensitive Liabilities.

    Assets are bucketed by months to the loan's next repricing event --
    approximated here as months remaining on fixed-rate term loans (a fixed
    loan "reprices" only at maturity/payoff).
    """

    def __init__(
        self,
        liability_weights: dict = None,
        total_liabilities: float = None,
        as_of_date: pd.Timestamp = None,
    ):

        super().__init__("Repricing Gap Tool")

        self.liability_weights = liability_weights or DEFAULT_LIABILITY_WEIGHTS
        self.total_liabilities = total_liabilities
        self.as_of_date = as_of_date or pd.Timestamp.now()

    def _assign_bucket(self, months_remaining: float) -> str:

        for label, lower, upper in REPRICING_BUCKETS:
            if upper is None or months_remaining < upper:
                if months_remaining >= lower:
                    return label

        return REPRICING_BUCKETS[-1][0]

    def _resolve_months_remaining(self, loans: pd.DataFrame) -> pd.Series:
        """
        Supports three input shapes, checked in order:
          1. A precomputed 'months_remaining' column, used as-is.
          2. 'term_months' + 'issue_date' -- months remaining is derived.
          3. 'term' (0/1 encoded, matching the PD/LGD notebooks) + 'issue_date'.
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

        issue_date = pd.to_datetime(loans["issue_date"])
        months_elapsed = (self.as_of_date - issue_date) / pd.Timedelta(days=30.44)

        if "term_months" in loans.columns:
            term_months = loans["term_months"]
        elif "term" in loans.columns:
            term_months = loans["term"].map(TERM_CODE_TO_MONTHS)
        else:
            raise ValueError(
                "loans needs 'term_months' (raw) or 'term' (0/1 encoded) "
                "alongside 'issue_date' to derive months_remaining"
            )

        return (term_months - months_elapsed).clip(lower=0)

    def run(self, loans: pd.DataFrame) -> RepricingGapResult:

        if "exposure_at_default" not in loans.columns:
            raise ValueError("loans must include an 'exposure_at_default' column")

        loans = loans.copy()
        loans["months_remaining"] = self._resolve_months_remaining(loans)
        loans["bucket"] = loans["months_remaining"].apply(self._assign_bucket)

        rsa_by_bucket = loans.groupby("bucket")["exposure_at_default"].sum()
        total_rsa = float(rsa_by_bucket.sum())

        # Synthetic liabilities are sized relative to total assets unless an
        # explicit total_liabilities figure is supplied.
        total_liabilities = (
            self.total_liabilities if self.total_liabilities is not None else total_rsa
        )

        buckets = []
        for label, _, _ in REPRICING_BUCKETS:

            rsa = float(rsa_by_bucket.get(label, 0.0))
            rsl = float(total_liabilities * self.liability_weights.get(label, 0.0))

            buckets.append(
                BucketGap(
                    bucket_label=label,
                    rate_sensitive_assets=rsa,
                    rate_sensitive_liabilities=rsl,
                    gap=rsa - rsl,
                )
            )

        total_rsl = sum(b.rate_sensitive_liabilities for b in buckets)

        return RepricingGapResult(
            buckets=buckets,
            total_rate_sensitive_assets=total_rsa,
            total_rate_sensitive_liabilities=total_rsl,
            net_gap=total_rsa - total_rsl,
        )
