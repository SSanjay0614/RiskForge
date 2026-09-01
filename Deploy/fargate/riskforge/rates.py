"""
The `rates` branch: agents/interest_rate_concentration_agent.py, as a Fargate
task.

    rows from S3 -> RepricingGapTool          (interest rate risk)
                 -> ConcentrationTool x2      (HHI by purpose, HHI by state)
                 -> aggregates to S3

Both tools are the repository's, imported unchanged. This module is the agent's
orchestration and nothing more, which is why it is short: the agent's own value
was that it ran three metrics defensively rather than as one unit, and that is
what is carried across.

**Raw rows, not engineered ones.** This is the distinction the local agent's
docstring makes and it survives here intact: FeatureEngineeringTool frequency
encodes addr_state, one-hot encodes purpose and turns sub_grade into a float, so
an engineered frame has no column to segment on. HHI by state needs 'CA', not
0.1348. So this branch reads the same CSV the score branch reads and does not
engineer it -- which is also why the two branches are genuinely independent and
can run at the same time.

**Each metric is guarded separately.** A generated SELECT that happens not to
return issue_date makes the repricing gap impossible and says nothing about
whether HHI can be computed. Locally that is a caught ValueError and a logged
skip; here it is a null with the reason recorded in the output, because a task
that failed outright would take the concentration numbers down with it.
"""
from tools.concentration_tool import ConcentrationTool
from tools.repricing_gap_tool import RepricingGapTool

from utils.logger import logger


def _attempt(label, fn, skipped):
    """
    ValueError from these tools means "this metric needs a column the query did
    not return" -- the tools raise it deliberately, with the column named, rather
    than failing on a KeyError deeper down. Anything else is a real fault and is
    left to propagate: a task that quietly reports three nulls because pandas
    raised something unexpected is worse than one that fails.
    """
    try:
        return fn().model_dump()
    except ValueError as exc:
        logger.warning("rates | %s skipped | %s" % (label, exc))
        skipped[label] = str(exc)
        return None


def run(raw_df):
    """{"rate_metrics": {...}} with one key per metric, each either the result or
    null, plus the reasons for any nulls."""
    if raw_df is None or len(raw_df) == 0:
        logger.warning("rates | no rows from the query, nothing to measure")
        return {"rate_metrics": {}, "skipped_reason": "the query returned no rows"}

    gap_tool = RepricingGapTool()
    concentration_tool = ConcentrationTool()
    skipped = {}

    rate_metrics = {
        "repricing_gap": _attempt(
            "repricing_gap", lambda: gap_tool.run(raw_df), skipped),
        # Two dimensions rather than one because the question does not say which
        # segmentation it wants, and riskforge-compliance-check takes the worse of
        # the two against the single HHI limit -- so passing requires being
        # diversified by purpose AND by region, not on average.
        "concentration_by_purpose": _attempt(
            "concentration_by_purpose",
            lambda: concentration_tool.run(raw_df, segment_column="purpose"), skipped),
        "concentration_by_region": _attempt(
            "concentration_by_region",
            lambda: concentration_tool.run(raw_df, segment_column="addr_state"), skipped),
    }

    logger.info(
        "rates | computed | %s"
        % ", ".join(
            "%s=%s" % (key, "ok" if value else "skipped")
            for key, value in rate_metrics.items()
        )
    )

    if skipped:
        rate_metrics["skipped"] = skipped

    return {"rate_metrics": rate_metrics}
