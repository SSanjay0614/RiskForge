import joblib
import pandas as pd

from tools.base_tool import BaseTool

from dmodels.expected_loss_result import ExpectedLossResult

from config import (
    PD_MODEL_PATH,
    PD_FEATURE_NAMES_PATH,
    PD_TIER_CUTOFFS_PATH,
    LGD_MODEL_PATH,
    LGD_FEATURE_NAMES_PATH,
)


class ExpectedLossTool(BaseTool):
    """
    Computes portfolio/segment-level Expected Loss: EL = PD x LGD x EAD.

    PD comes from the calibrated behavioral PD model, LGD from the trained
    LGD regressor, EAD from the loan's own outstanding balance (no model
    needed for term loans -- see PD/LGD READMEs for the reasoning).

    Input rows must already be feature-engineered per
    01_feature_engineering.ipynb, with an added `exposure_at_default` column.
    """

    def __init__(
        self,
        pd_model_path: str = PD_MODEL_PATH,
        pd_feature_names_path: str = PD_FEATURE_NAMES_PATH,
        pd_tier_cutoffs_path: str = PD_TIER_CUTOFFS_PATH,
        lgd_model_path: str = LGD_MODEL_PATH,
        lgd_feature_names_path: str = LGD_FEATURE_NAMES_PATH,
    ):
        super().__init__("Expected Loss Tool")

        self.pd_model = joblib.load(pd_model_path)
        self.pd_feature_names = joblib.load(pd_feature_names_path)
        self.tier_cutoffs = joblib.load(pd_tier_cutoffs_path)

        self.lgd_model = joblib.load(lgd_model_path)
        self.lgd_feature_names = joblib.load(lgd_feature_names_path)

    def _assign_risk_tier(self, pd_value: float) -> str:

        if pd_value < self.tier_cutoffs["Low"]:
            return "Low"
        elif pd_value < self.tier_cutoffs["Medium"]:
            return "Medium"
        elif pd_value < self.tier_cutoffs["High"]:
            return "High"
        return "Very High"

    def _align_features(self, df: pd.DataFrame, feature_names: list) -> pd.DataFrame:

        df = df.copy()

        missing = [f for f in feature_names if f not in df.columns]
        for f in missing:
            df[f] = 0

        return df[feature_names]

    def run(self, loans: pd.DataFrame) -> ExpectedLossResult:

        if "exposure_at_default" not in loans.columns:
            raise ValueError("loans must include an 'exposure_at_default' column")

        X_pd = self._align_features(loans, self.pd_feature_names)
        X_lgd = self._align_features(loans, self.lgd_feature_names)

        predicted_pd = self.pd_model.predict_proba(X_pd)[:, 1]
        predicted_lgd = self.lgd_model.predict(X_lgd).clip(0, 1)

        ead = loans["exposure_at_default"].to_numpy()

        expected_loss_per_loan = predicted_pd * predicted_lgd * ead

        total_exposure = float(ead.sum())
        total_expected_loss = float(expected_loss_per_loan.sum())

        risk_tiers = pd.Series(predicted_pd).apply(self._assign_risk_tier)

        scored_df = loans.copy()
        scored_df["predicted_pd"] = predicted_pd
        scored_df["predicted_lgd"] = predicted_lgd

        return ExpectedLossResult(
            loan_count=len(loans),
            total_exposure=total_exposure,
            total_expected_loss=total_expected_loss,
            expected_loss_rate=(
                total_expected_loss / total_exposure if total_exposure > 0 else 0.0
            ),
            exposure_weighted_avg_pd=(
                float((predicted_pd * ead).sum() / total_exposure)
                if total_exposure > 0 else 0.0
            ),
            exposure_weighted_avg_lgd=(
                float((predicted_lgd * ead).sum() / total_exposure)
                if total_exposure > 0 else 0.0
            ),
            risk_tier_distribution=risk_tiers.value_counts(normalize=True).to_dict(),
            scored_df=scored_df,
        )