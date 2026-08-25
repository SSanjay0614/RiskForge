"""
Tests ComplianceAgent for real -- needs the real seeded credit_risk.db and
trained PD/LGD model artifacts. No Ollama needed: this agent makes zero
LLM calls (all citations are hardcoded, no RAG).
"""

from memory.state import RiskGraphState

from workflow.graph import graph


test_queries = [
    "What is our total outstanding balance, expected loss, and concentration risk for California loans?",
]

for query in test_queries:
    print("=" * 70)
    print(f"QUERY: {query}")
    print("=" * 70)

    result_state = graph.invoke(RiskGraphState(query=query, max_retries=3))

    print(f"credit_metrics: {result_state['credit_metrics']}")
    print()
    print(f"rate_metrics keys: {list(result_state['rate_metrics'].keys())}")
    print()
    print("COMPLIANCE RESULT:")
    compliance = result_state["compliance_result"]
    print(f"  any_breach: {compliance.get('any_breach')}")
    for flag in compliance.get("flags", []):
        print(
            f"  [{flag['source']:>10}] {flag['metric_name']:35s} "
            f"value={flag['value']:.4f} threshold={flag['threshold']:.4f} "
            f"breached={flag['breached']}"
        )
        if flag['breached']:
            print(f"      citation: {flag['citation']}")

    reg_cap = compliance.get("regulatory_capital")
    if reg_cap:
        print()
        print(f"  regulatory_capital: total_rwa={reg_cap['total_rwa']:,.2f}, "
              f"avg_risk_weight={reg_cap['avg_risk_weight_pct']:.1f}%")
        print(f"  citation: {compliance['regulatory_capital_citation'][:150]}...")
    print()