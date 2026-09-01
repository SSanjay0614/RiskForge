"""
The `score` branch: agents/credit_risk_agent.py, as a Fargate task.

    rows from S3 -> FeatureEngineeringTool -> PD and LGD endpoints
                 -> Expected Loss -> Basel III RWA -> aggregates to S3

Same three steps in the same order as CreditRiskAgent.run, with two differences,
both forced by where this runs rather than chosen:

  * PD and LGD are endpoints, so ExpectedLossTool is replaced by
    riskforge/scoring.py. Nothing else about the calculation moves.
  * **Regulatory capital is computed here.** Locally it belongs to the Compliance
    Agent, which has state.scored_df in the same process. In the deployed pipeline
    the compliance step is riskforge-compliance-check, a Lambda that receives
    metrics in its event -- and RWA is a sum over per-loan PD, LGD and EAD, so
    sending it there would mean shipping 878,000 scored loans to a Lambda to get
    one number back. That function's docstring already says RWA stays in the
    Fargate task; this is the task it stays in.

The returned dict is credit_metrics as riskforge-compliance-check expects to
receive it -- `expected_loss_rate`, `exposure_weighted_avg_pd` and
`exposure_weighted_avg_lgd` are the three keys its METRIC_LOOKUP reads, and they
keep those names for that reason.

Nothing per-loan is returned. The scored frame exists in this process and is
dropped when it exits; what leaves is sums, shares and weighted averages. See
riskforge/outputs.py, which enforces that rather than trusting it.
"""

from tools.regulatory_capital_tool import RegulatoryCapitalTool

from utils.logger import logger

from . import features
from .scoring import Scorer

# Reported alongside the count-based distribution because they answer different
# questions: 40% of loans in the Low tier and 40% of exposure in the Low tier are
# not the same portfolio, and a risk report that gives only the first invites the
# reader to assume the second.
TIER_ORDER = ["Low", "Medium", "High", "Very High"]


def _exposure_by_tier(scored_df):
    if "risk_tier" not in scored_df.columns:
        return {}
    grouped = scored_df.groupby("risk_tier")
    exposure = grouped["exposure_at_default"].sum()
    losses = grouped["expected_loss"].sum()
    total = float(exposure.sum())
    out = {}
    for tier in TIER_ORDER:
        if tier not in exposure.index:
            continue
        tier_exposure = float(exposure[tier])
        out[tier] = {
            "loan_count": int(grouped.size()[tier]),
            "exposure": tier_exposure,
            "exposure_share": tier_exposure / total if total > 0 else 0.0,
            "expected_loss": float(losses[tier]),
            "expected_loss_rate": (
                float(losses[tier]) / tier_exposure if tier_exposure > 0 else 0.0
            ),
        }
    return out


def run(raw_df, pd_endpoint, lgd_endpoint, batch_rows=None, workers=None):
    """
    {"credit_metrics": {...}, "regulatory_capital": {...}} -- or credit_metrics
    empty with a reason, if nothing survived to score.

    Returning an empty dict rather than raising on an empty population is carried
    across from CreditRiskAgent, and it is the right behaviour for the same
    reason: a query that legitimately matched no loans, or whose rows were all
    dropped for non-positive DTI, is a valid answer to a question, not a task
    failure. Step Functions should report "no loans matched", not "the branch
    crashed".
    """
    if raw_df is None or len(raw_df) == 0:
        logger.warning("credit | no rows from the query, nothing to score")
        return {"credit_metrics": {}, "regulatory_capital": None,
                "skipped_reason": "the query returned no rows"}

    engineered, diagnostics = features.engineer(raw_df)

    if len(engineered) == 0:
        logger.warning("credit | no rows survived feature engineering")
        return {"credit_metrics": {}, "regulatory_capital": None,
                "skipped_reason": "no rows survived feature engineering",
                "diagnostics": diagnostics}

    kwargs = {}
    if batch_rows:
        kwargs["batch_rows"] = batch_rows
    if workers:
        kwargs["workers"] = workers
    scorer = Scorer(pd_endpoint, lgd_endpoint, **kwargs)
    el = scorer.score(engineered)

    logger.info(
        "credit | expected loss | exposure=%.2f el=%.2f rate=%.4f"
        % (el.total_exposure, el.total_expected_loss, el.expected_loss_rate)
    )

    capital = RegulatoryCapitalTool().run(el.scored_df)

    logger.info(
        "credit | regulatory capital | rwa=%.2f risk_weight=%.1f%%"
        % (capital.total_rwa, capital.avg_risk_weight_pct)
    )

    credit_metrics = el.model_dump(exclude={"scored_df"})
    # The row accounting rides inside credit_metrics so the interface can explain
    # why fewer loans were scored than retrieved, instead of showing two numbers
    # that silently disagree. Carried across from CreditRiskAgent, including the
    # reason it is here and not on ExpectedLossResult.
    credit_metrics.update(diagnostics)
    credit_metrics["exposure_by_risk_tier"] = _exposure_by_tier(el.scored_df)
    credit_metrics["pd_endpoint"] = pd_endpoint
    credit_metrics["lgd_endpoint"] = lgd_endpoint

    return {
        "credit_metrics": credit_metrics,
        "regulatory_capital": capital.model_dump(),
    }
