from typing import Dict, Any, List

from tools.risk_limits_tool import RiskLimitsTool
from tools.regulatory_capital_tool import RegulatoryCapitalTool

from memory.state import RiskGraphState

from dmodels.compliance_result import ComplianceFlag, ComplianceResult

from utils.logger import logger


# Maps each Risk_Limits metric_name to (a) where its computed value lives in
# state, and (b) whether it's a 'max' ceiling (internal policy) or a 'min'
# floor (Basel-sourced regulatory minimum -- breached if the value falls
# BELOW the threshold, not above).
METRIC_LOOKUP = {
    "max_expected_loss_rate": {
        "getter": lambda state: state.credit_metrics.get("expected_loss_rate"),
        "direction": "max",
    },
    "max_hhi_10000_scale": {
        "getter": lambda state: max(
            (state.rate_metrics.get("concentration_by_purpose") or {}).get("hhi_score_10000_scale", 0),
            (state.rate_metrics.get("concentration_by_region") or {}).get("hhi_score_10000_scale", 0),
        ) if state.rate_metrics else None,
        "direction": "max",
    },
    "pd_floor_retail_other": {
        "getter": lambda state: state.credit_metrics.get("exposure_weighted_avg_pd"),
        "direction": "min",
    },
    "lgd_floor_retail_unsecured_other": {
        "getter": lambda state: state.credit_metrics.get("exposure_weighted_avg_lgd"),
        "direction": "min",
    },
}

# All citations are fully hardcoded -- no retrieval involved anywhere in
# this agent. Basel-sourced limits (pd_floor_retail_other,
# lgd_floor_retail_unsecured_other) already carry their exact source
# paragraph in Risk_Limits.description (see Database/add_basel_limits.py),
# since that's literally where the threshold value came from. Internal
# policy limits (max_expected_loss_rate, max_hhi_10000_scale) aren't
# derived from Basel text at all, so they get an honest static note instead
# of a manufactured citation.
BREACH_NOTES = {
    "max_expected_loss_rate": (
        "Internal risk policy threshold (not itself a Basel limit). Elevated PD/LGD "
        "estimates directly increase risk-weighted assets and regulatory capital "
        "requirements under Basel III CRE31.16 -- see regulatory_capital_citation below."
    ),
    "max_hhi_10000_scale": (
        "Internal risk policy threshold -- not derived from Basel III. Basel's credit "
        "risk framework (CRE30-32) does not set portfolio-level sector concentration "
        "limits; concentration risk is addressed under each bank's own risk appetite "
        "framework, and separately, for single-counterparty exposures, under Basel's "
        "Large Exposures framework (not applicable to sector-level retail concentration)."
    ),
}


class ComplianceAgent:
    """
    Checks computed portfolio metrics (from CreditRiskAgent and
    InterestRateConcentrationAgent, which must both have completed -- this
    agent sits at the fan-in point after their parallel branches) against
    internal policy limits and Basel III regulatory floors.

    All citations are fully hardcoded, no retrieval involved -- this
    project's Basel corpus is small and fixed (CRE30-32), so every fact
    this agent needs is already known exactly rather than needing to be
    rediscovered per query. RAG was evaluated for this (see project RAG
    notebook/discussion) and deliberately dropped: it never had its own
    graph node or user-facing entry point, was only ever a dependency of
    this one internal step, and added retrieval-reliability risk for
    content that's small and static enough to just know directly.

    Limit checking itself is deterministic -- no LLM judgment anywhere in
    this agent.
    """

    def __init__(
        self,
        risk_limits_tool: RiskLimitsTool = None,
        regulatory_capital_tool: RegulatoryCapitalTool = None,
    ):
        self.name = "Compliance Agent"
        self.logger = logger

        self.risk_limits_tool = risk_limits_tool or RiskLimitsTool()
        self.regulatory_capital_tool = regulatory_capital_tool or RegulatoryCapitalTool()

    def run(self, state: RiskGraphState) -> Dict[str, Any]:

        limits = self.risk_limits_tool.run()

        flags: List[ComplianceFlag] = []

        for limit in limits:
            lookup = METRIC_LOOKUP.get(limit.metric_name)
            if lookup is None:
                self.logger.warning(f"{self.name} | no lookup defined for {limit.metric_name}, skipping")
                continue

            value = lookup["getter"](state)
            if value is None:
                self.logger.warning(f"{self.name} | value unavailable for {limit.metric_name}, skipping")
                continue

            direction = lookup["direction"]
            breached = (value > limit.threshold) if direction == "max" else (value < limit.threshold)

            citation = None
            if breached:
                citation = limit.description if limit.source == "basel_iii" else BREACH_NOTES.get(limit.metric_name)

            flags.append(ComplianceFlag(
                metric_name=limit.metric_name,
                value=value,
                threshold=limit.threshold,
                source=limit.source,
                breached=breached,
                direction=direction,
                citation=citation,
            ))

            self.logger.info(
                f"{self.name} | {limit.metric_name} | value={value} threshold={limit.threshold} "
                f"breached={breached} citation={'yes' if citation else 'no'}"
            )

        regulatory_capital = None
        if state.scored_df is not None and len(state.scored_df) > 0:
            try:
                capital_result = self.regulatory_capital_tool.run(state.scored_df)
                regulatory_capital = capital_result.model_dump()
                self.logger.info(
                    f"{self.name} | regulatory capital | "
                    f"total_rwa={capital_result.total_rwa:.2f} "
                    f"avg_risk_weight={capital_result.avg_risk_weight_pct:.1f}%"
                )
            except ValueError as e:
                self.logger.warning(f"{self.name} | regulatory capital skipped | {e}")

        result = ComplianceResult(
            flags=flags,
            any_breach=any(f.breached for f in flags),
            regulatory_capital=regulatory_capital,
        )

        return {"compliance_result": result.model_dump()}