import numpy as np
import pandas as pd
from scipy.stats import norm

from tools.base_tool import BaseTool

from dmodels.regulatory_capital_result import RegulatoryCapitalResult


class RegulatoryCapitalTool(BaseTool):
    """
    Computes Basel III IRB regulatory capital (correlation R, capital
    requirement K, and RWA) for "Other Retail" exposures, per CRE31.16 --
    verified letter-for-letter against the primary BIS source before
    implementation.

    Correlation = R = 0.03 * (1 - e^(-35*PD))/(1 - e^(-35))
                     + 0.16 * [1 - (1 - e^(-35*PD))/(1 - e^(-35))]

    K = LGD * N[ (G(PD) + sqrt(R/(1-R)) * G(0.999)) / sqrt(1-R) ] - PD*LGD
        (retail has NO maturity adjustment -- confirmed via BIS QIS3 FAQ:
        maturity is "subsumed in the correlation assumption" for retail)

    RWA = K * 12.5 * EAD

    Applies specifically to "Other Retail" exposures (unsecured, not
    mortgages, not QRRE/revolving) -- the correct class for unsecured
    personal installment loans. N is the standard normal CDF, G its inverse.
    """

    def __init__(self):
        super().__init__("Regulatory Capital Tool")

    def _correlation(self, pd_values: np.ndarray) -> np.ndarray:

        term = (1 - np.exp(-35 * pd_values)) / (1 - np.exp(-35))
        return 0.03 * term + 0.16 * (1 - term)

    def _capital_requirement(
        self, pd_values: np.ndarray, lgd_values: np.ndarray, r_values: np.ndarray
    ) -> np.ndarray:

        g_pd = norm.ppf(pd_values)
        g_999 = norm.ppf(0.999)

        inner = (g_pd + np.sqrt(r_values / (1 - r_values)) * g_999) / np.sqrt(1 - r_values)

        k = lgd_values * norm.cdf(inner) - pd_values * lgd_values

        # If K is negative, the exposure requires zero regulatory capital.
        return np.clip(k, a_min=0, a_max=None)

    def run(
        self, loans: pd.DataFrame, pd_col: str = "predicted_pd", lgd_col: str = "predicted_lgd"
    ) -> RegulatoryCapitalResult:

        required = {pd_col, lgd_col, "exposure_at_default"}
        missing = required - set(loans.columns)
        if missing:
            raise ValueError(f"loans is missing required columns: {missing}")

        pd_values = loans[pd_col].to_numpy()
        lgd_values = loans[lgd_col].to_numpy()
        ead_values = loans["exposure_at_default"].to_numpy()

        # PD floor per CRE32.58 -- 0.05% for "other" (non-QRRE) retail
        # exposures. Applied here since K/R are only meaningful for
        # regulator-compliant PD inputs.
        pd_values = np.clip(pd_values, a_min=0.0005, a_max=None)

        r_values = self._correlation(pd_values)
        k_values = self._capital_requirement(pd_values, lgd_values, r_values)
        rwa_values = k_values * 12.5 * ead_values

        total_ead = float(ead_values.sum())
        total_rwa = float(rwa_values.sum())

        return RegulatoryCapitalResult(
            loan_count=len(loans),
            total_ead=total_ead,
            total_rwa=total_rwa,
            total_capital_requirement_8pct=total_rwa * 0.08,
            exposure_weighted_avg_correlation=(
                float((r_values * ead_values).sum() / total_ead) if total_ead > 0 else 0.0
            ),
            exposure_weighted_avg_k=(
                float((k_values * ead_values).sum() / total_ead) if total_ead > 0 else 0.0
            ),
            avg_risk_weight_pct=(
                float(total_rwa / total_ead * 100) if total_ead > 0 else 0.0
            ),
        )
