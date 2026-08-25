from typing import Dict, Any

from tools.repricing_gap_tool import RepricingGapTool
from tools.concentration_tool import ConcentrationTool

from memory.state import RiskGraphState

from utils.logger import logger


class InterestRateConcentrationAgent:
    """
    Consumes the RAW loan rows retrieved by the Data Agent (not the
    ML-engineered feature set -- FeatureEngineeringTool one-hot/frequency/
    ordinal-encodes exactly the columns this agent needs in human-readable
    form: purpose, addr_state, sub_grade).

    Computes repricing gap (interest rate risk) and concentration (HHI) on
    two standard segment dimensions -- sector (purpose) and region
    (addr_state) -- since the query itself doesn't specify which
    segmentation the user wants.

    Runs in PARALLEL with CreditRiskAgent -- returns a partial update dict,
    not the full state, and does NOT write `current_agent` (see
    CreditRiskAgent docstring for why).

    Each metric is computed independently and defensively: if a query's SQL
    happens not to include the columns a particular metric needs (e.g. no
    issue_date for repricing gap), that one metric is skipped with a logged
    reason rather than failing the whole agent.
    """

    def __init__(
        self,
        repricing_gap_tool: RepricingGapTool = None,
        concentration_tool: ConcentrationTool = None,
    ):
        self.name = "Interest Rate & Concentration Agent"
        self.logger = logger

        self.repricing_gap_tool = repricing_gap_tool or RepricingGapTool()
        self.concentration_tool = concentration_tool or ConcentrationTool()

    def run(self, state: RiskGraphState) -> Dict[str, Any]:

        if state.rows_df is None or len(state.rows_df) == 0:
            self.logger.warning(f"{self.name} | no rows available from Data Agent, skipping")
            return {"rate_metrics": {}}

        rate_metrics: Dict[str, Any] = {}

        try:
            gap_result = self.repricing_gap_tool.run(state.rows_df)
            rate_metrics["repricing_gap"] = gap_result.model_dump()
        except ValueError as e:
            self.logger.warning(f"{self.name} | repricing gap skipped | {e}")
            rate_metrics["repricing_gap"] = None

        try:
            purpose_result = self.concentration_tool.run(state.rows_df, segment_column="purpose")
            rate_metrics["concentration_by_purpose"] = purpose_result.model_dump()
        except ValueError as e:
            self.logger.warning(f"{self.name} | concentration by purpose skipped | {e}")
            rate_metrics["concentration_by_purpose"] = None

        try:
            region_result = self.concentration_tool.run(state.rows_df, segment_column="addr_state")
            rate_metrics["concentration_by_region"] = region_result.model_dump()
        except ValueError as e:
            self.logger.warning(f"{self.name} | concentration by region skipped | {e}")
            rate_metrics["concentration_by_region"] = None

        self.logger.info(
            f"{self.name} | computed | "
            f"repricing_gap={'ok' if rate_metrics['repricing_gap'] else 'skipped'} | "
            f"concentration_by_purpose={'ok' if rate_metrics['concentration_by_purpose'] else 'skipped'} | "
            f"concentration_by_region={'ok' if rate_metrics['concentration_by_region'] else 'skipped'}"
        )

        return {"rate_metrics": rate_metrics}
