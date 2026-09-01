"""
riskforge-compliance-check -- deterministic limit checking, no LLM anywhere.

A direct port of agents/compliance_agent.py's limit-checking half. The thresholds
are read from the `risk_limits` table at invocation time rather than baked in, so
the table stays the single source of truth; the computed metrics arrive in the
event, because the agent that computes them (CreditRiskAgent /
InterestRateConcentrationAgent, Phase 10) holds an 878k-row scored frame that
cannot travel through a Lambda payload and should not need to.

**What deliberately did not come across:** `RegulatoryCapitalTool`. It needs
`state.scored_df` -- per-loan PD, LGD and EAD for the whole portfolio -- to sum
risk-weighted assets. Sending that here would mean shipping the portfolio to a
Lambda to get one number back, so RWA stays in the Fargate task that already has
the frame in memory. The `regulatory_capital` key in the response is therefore
always null with a stated reason, rather than quietly absent.

The METRIC_LOOKUP directions are the part worth not paraphrasing: a 'max' limit
is an internal ceiling and breaches upward, a 'min' limit is a Basel-sourced
floor and breaches *downward*. Getting that backwards produces a compliance
report that is confidently wrong in the direction that matters.

Event:
    {"credit_metrics": {"expected_loss_rate": 0.031, "exposure_weighted_avg_pd": 0.09,
                        "exposure_weighted_avg_lgd": 0.42},
     "rate_metrics": {"concentration_by_purpose": {"hhi_score_10000_scale": 2210},
                      "concentration_by_region": {"hhi_score_10000_scale": 640},
                      "repricing_gap": {"loan_to_deposit_ratio": 0.88}}}

Response:
    {"success": true, "any_breach": true, "flags": [...], "skipped": [...],
     "regulatory_capital": null, "regulatory_capital_note": "..."}
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared"))

import db  # noqa: E402

def _hhi(rate_metrics):
    """The worse of the two concentration readings. One limit, two places a
    portfolio can be concentrated -- taking the max means passing requires being
    diversified by purpose *and* by region, not on average."""
    if not rate_metrics:
        return None
    scores = [
        (rate_metrics.get(key) or {}).get("hhi_score_10000_scale")
        for key in ("concentration_by_purpose", "concentration_by_region")
    ]
    scores = [s for s in scores if s is not None]
    return max(scores) if scores else None


# Maps each risk_limits.metric_name to where its computed value arrives in the
# event, and whether it is a 'max' ceiling (internal policy, breached above) or a
# 'min' floor (Basel-sourced regulatory minimum, breached BELOW).
METRIC_LOOKUP = {
    "max_expected_loss_rate": {
        "getter": lambda e: (e.get("credit_metrics") or {}).get("expected_loss_rate"),
        "direction": "max",
    },
    "max_hhi_10000_scale": {
        "getter": lambda e: _hhi(e.get("rate_metrics")),
        "direction": "max",
    },
    "pd_floor_retail_other": {
        "getter": lambda e: (e.get("credit_metrics") or {}).get("exposure_weighted_avg_pd"),
        "direction": "min",
    },
    "lgd_floor_retail_unsecured_other": {
        "getter": lambda e: (e.get("credit_metrics") or {}).get("exposure_weighted_avg_lgd"),
        "direction": "min",
    },
    "max_loan_to_deposit_ratio": {
        "getter": lambda e: (
            ((e.get("rate_metrics") or {}).get("repricing_gap") or {}).get("loan_to_deposit_ratio")
        ),
        "direction": "max",
    },
}

# Copied verbatim from agents/compliance_agent.py -- these are citations, and a
# citation that has drifted from the one the local pipeline prints is worse than
# no citation. Every one is hardcoded; no retrieval is involved anywhere here.
# Basel-sourced limits already carry their exact source paragraph in
# risk_limits.description, since that is literally where the threshold came from,
# so they use that instead of anything below. Internal policy limits are not
# derived from Basel text at all and get an honest static note rather than a
# manufactured citation.
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
    "max_loan_to_deposit_ratio": (
        "Internal funding-risk threshold, not a Basel III limit -- Basel addresses funding "
        "stability through the Net Stable Funding Ratio (NSFR) rather than a loan-to-deposit "
        "cap. Note also that the deposit figure is a documented assumption: this portfolio "
        "holds loans only, so the deposit book is modelled at a fixed multiple of the loan "
        "book (see RepricingGapTool). The ratio is therefore illustrative of the check, not "
        "an observed funding position."
    ),
}

REGULATORY_CAPITAL_NOTE = (
    "Not computed here. Risk-weighted assets need per-loan PD, LGD and EAD for the whole "
    "878k-row portfolio, which stays in the scoring task that already holds it rather than "
    "being shipped to a Lambda for one number."
)

def _read_limits():
    # 10 seconds, not the shared default of 25. This function reads a five-row
    # table, so the budget is not sizing -- it is staying inside this function's
    # own 30-second Lambda timeout. db.connect() derives the socket timeout from
    # the statement timeout and keeps it above, so asking for 25 here would put
    # the socket at 30 and let the function be killed mid-call instead of
    # returning the error it was built to return.
    connection = db.connect(statement_timeout_ms=10_000)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT metric_name, threshold, source, description FROM risk_limits "
            "ORDER BY metric_name"
        )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        connection.close()

    return [
        {
            "metric_name": r[0],
            "threshold": float(r[1]),
            "source": r[2] or "",
            "description": r[3] or "",
        }
        for r in rows
    ]


def lambda_handler(event, context):
    event = event or {}

    try:
        limits = _read_limits()
    except Exception as exc:
        return {"success": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    flags = []
    skipped = []

    for limit in limits:
        name = limit["metric_name"]
        lookup = METRIC_LOOKUP.get(name)
        if lookup is None:
            skipped.append({"metric_name": name, "reason": "no lookup defined for this metric"})
            continue

        value = lookup["getter"](event)
        if value is None:
            skipped.append({"metric_name": name, "reason": "value not supplied in the event"})
            continue

        value = float(value)
        threshold = limit["threshold"]
        direction = lookup["direction"]
        breached = (value > threshold) if direction == "max" else (value < threshold)

        citation = None
        if breached:
            citation = (
                limit["description"] if limit["source"] == "basel_iii" else BREACH_NOTES.get(name)
            )

        flags.append(
            {
                "metric_name": name,
                "value": value,
                "threshold": threshold,
                "source": limit["source"],
                "breached": breached,
                "direction": direction,
                "citation": citation,
            }
        )

    return {
        "success": True,
        "any_breach": any(f["breached"] for f in flags),
        "flags": flags,
        # Surfaced rather than logged: a limit that was skipped is a limit that
        # was not checked, and a compliance report that quietly omits one reads
        # identically to one that passed it.
        "skipped": skipped,
        "limits_checked": len(flags),
        "limits_in_table": len(limits),
        "regulatory_capital": None,
        "regulatory_capital_note": REGULATORY_CAPITAL_NOTE,
    }
