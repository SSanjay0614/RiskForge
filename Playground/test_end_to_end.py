"""
Full real end-to-end test of the complete graph:
Data Agent -> [Credit Risk Agent + Interest Rate & Concentration Agent] -> Compliance Agent

Needs: Ollama running (for the Data Agent's SQL generation/evaluation
steps only -- Compliance Agent makes no LLM calls, all citations are
hardcoded), real seeded credit_risk.db, and real trained PD/LGD model
artifacts. Run `python -m Database.add_basel_limits` first if you haven't
already, so the Basel PD/LGD floor checks have thresholds to compare against.
"""

from memory.state import RiskGraphState

from workflow.graph import graph


def print_compliance(compliance: dict):
    if not compliance:
        print("  (empty -- Compliance Agent never ran, e.g. guard rejected the query)")
        return

    print(f"  any_breach: {compliance.get('any_breach')}")
    for flag in compliance.get("flags", []):
        print(
            f"  [{flag['source']:>10}] {flag['metric_name']:35s} "
            f"value={flag['value']:.4f} threshold={flag['threshold']:.4f} "
            f"breached={flag['breached']} citation={'yes' if flag['citation'] else 'no'}"
        )

    reg_cap = compliance.get("regulatory_capital")
    if reg_cap:
        print(f"  regulatory_capital: total_rwa={reg_cap['total_rwa']:,.2f}, "
              f"avg_risk_weight={reg_cap['avg_risk_weight_pct']:.1f}%, "
              f"capital_req_8pct={reg_cap['total_capital_requirement_8pct']:,.2f}")
    else:
        print("  regulatory_capital: not computed (no scored loan data)")


test_cases = [
    {
        "label": "Normal query -- should succeed through the full pipeline",
        "query": "What is our total outstanding balance, expected loss, and concentration risk for California loans?",
    },
    {
        "label": "Query likely to trigger a compliance breach (high-risk grade)",
        "query": "What is our expected loss and concentration for loans with a sub_grade of E5?",
    },
    {
        "label": "Guard should reject this -- unrelated to the schema",
        "query": "What's the weather like today?",
    },
    {
        "label": "Lets see...",
        "query": "How many loans do we have with a sub_grade of B3?",
    },
]

for case in test_cases:
    print("=" * 70)
    print(case["label"])
    print(f"QUERY: {case['query']}")
    print("=" * 70)

    result_state = graph.invoke(RiskGraphState(query=case["query"], max_retries=3))

    print(f"data_agent_result: {result_state['data_agent_result']}")
    print()

    print("CREDIT RISK:")
    print(f"  {result_state['credit_metrics'] or '(skipped -- no data)'}")
    if result_state.get("scored_df") is not None:
        print(f"  scored_df shape: {result_state['scored_df'].shape}")
    print()

    print("INTEREST RATE & CONCENTRATION:")
    rate_metrics = result_state["rate_metrics"]
    if rate_metrics:
        gap = rate_metrics.get("repricing_gap")
        print(f"  repricing_gap net_gap: {gap['net_gap'] if gap else 'skipped'}")
        purpose = rate_metrics.get("concentration_by_purpose")
        print(f"  concentration_by_purpose: {purpose['diversification_level'] if purpose else 'skipped'}")
        region = rate_metrics.get("concentration_by_region")
        print(f"  concentration_by_region: {region['diversification_level'] if region else 'skipped'}")
    else:
        print("  (skipped -- no data)")
    print()

    print("COMPLIANCE:")
    print_compliance(result_state["compliance_result"])
    print()