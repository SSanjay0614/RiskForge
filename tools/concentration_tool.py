import pandas as pd

from tools.base_tool import BaseTool

from dmodels.concentration_result import ConcentrationResult


# DOJ merger-guideline convention, applied here to portfolio segment
# concentration rather than market concentration (same underlying metric):
# < 1500 unconcentrated, 1500-2500 moderately concentrated, > 2500 highly
# concentrated, on the 0-10000 scale.
UNCONCENTRATED_THRESHOLD = 1500
MODERATE_THRESHOLD = 2500


class ConcentrationTool(BaseTool):
    """
    Computes portfolio concentration risk via the Herfindahl-Hirschman Index:
    HHI = sum((exposure share of segment i) ** 2), across a chosen segment
    column (e.g. sector, region, borrower type).
    """

    def __init__(self):
        super().__init__("Concentration Tool")

    def _diversification_label(self, hhi_10000: float) -> str:

        if hhi_10000 < UNCONCENTRATED_THRESHOLD:
            return "Diversified"
        elif hhi_10000 < MODERATE_THRESHOLD:
            return "Moderately Concentrated"
        return "Highly Concentrated"

    def run(self, loans: pd.DataFrame, segment_column: str) -> ConcentrationResult:

        required_cols = {"exposure_at_default", segment_column}
        missing = required_cols - set(loans.columns)
        if missing:
            raise ValueError(f"loans is missing required columns: {missing}")

        exposure_by_segment = loans.groupby(segment_column)["exposure_at_default"].sum()
        total_exposure = exposure_by_segment.sum()

        if total_exposure == 0:
            return ConcentrationResult(segment_column=segment_column)

        shares = exposure_by_segment / total_exposure

        hhi_fractional = float((shares ** 2).sum())
        hhi_10000 = hhi_fractional * 10000

        return ConcentrationResult(
            segment_column=segment_column,
            hhi_score=hhi_fractional,
            hhi_score_10000_scale=hhi_10000,
            diversification_level=self._diversification_label(hhi_10000),
            segment_shares=shares.to_dict(),
        )
