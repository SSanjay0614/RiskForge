from typing import Dict, Any

from tools.feature_engineering_tool import FeatureEngineeringTool
from tools.expected_loss_tool import ExpectedLossTool

from memory.state import RiskGraphState

from utils.logger import logger


class CreditRiskAgent:
    """
    Consumes the raw loan rows retrieved by the Data Agent, runs them
    through feature engineering, and computes portfolio/segment-level
    Expected Loss.

    Single node, no branching -- unlike the Data Agent, there's no retry or
    evaluation judgment needed here: feature engineering and EL calculation
    are both deterministic once rows_df exists, so this runs as one
    straight-line step rather than a multi-node subgraph.

    Runs in PARALLEL with InterestRateConcentrationAgent (both depend only
    on the Data Agent's output, not on each other) -- returns a partial
    update dict, not the full state, and deliberately does NOT write
    `current_agent`, since that field has no single well-defined value
    while two agents are genuinely running at once.
    """

    def __init__(
        self,
        feature_engineering_tool: FeatureEngineeringTool = None,
        expected_loss_tool: ExpectedLossTool = None,
    ):
        self.name = "Credit Risk Agent"
        self.logger = logger

        self.feature_engineering_tool = feature_engineering_tool or FeatureEngineeringTool()
        self.expected_loss_tool = expected_loss_tool or ExpectedLossTool()

    def run(self, state: RiskGraphState) -> Dict[str, Any]:

        if state.rows_df is None or len(state.rows_df) == 0:
            self.logger.warning(f"{self.name} | no rows available from Data Agent, skipping")
            return {"credit_metrics": {}}

        # FeatureEngineeringTool validates against the schema's raw column
        # names (outstanding_balance) -- rename to exposure_at_default only
        # AFTER engineering, right before ExpectedLossTool, which expects
        # that name specifically.
        fe_result = self.feature_engineering_tool.run(state.rows_df)

        self.logger.info(
            f"{self.name} | feature engineering | "
            f"input={fe_result.input_row_count} output={fe_result.output_row_count} "
            f"dropped={fe_result.rows_dropped} reasons={fe_result.dropped_reason_counts}"
        )

        if fe_result.output_row_count == 0:
            self.logger.warning(f"{self.name} | no rows survived feature engineering")
            return {"credit_metrics": {}, "engineered_df": fe_result.engineered_df}

        engineered = fe_result.engineered_df.copy()
        if "exposure_at_default" not in engineered.columns and "outstanding_balance" in engineered.columns:
            engineered["exposure_at_default"] = engineered["outstanding_balance"]

        el_result = self.expected_loss_tool.run(engineered)

        self.logger.info(
            f"{self.name} | expected loss | "
            f"total_exposure={el_result.total_exposure:.2f} "
            f"total_expected_loss={el_result.total_expected_loss:.2f} "
            f"rate={el_result.expected_loss_rate:.4f}"
        )

        return {
            "engineered_df": fe_result.engineered_df,
            "scored_df": el_result.scored_df,
            "credit_metrics": el_result.model_dump(exclude={"scored_df"}),
        }