from memory.state import RiskGraphState

from workflow.graph import graph


test_queries = [
    "What is our total outstanding balance and expected loss for loans in California?",
    "What is the expected loss for loans with a sub_grade of B3?",
]

for query in test_queries:

    print("=" * 70)
    print(f"QUERY: {query}")
    print("=" * 70)

    result_state = graph.invoke(RiskGraphState(query=query, max_retries=3))

    print(f"data_agent_result: {result_state['data_agent_result']}")
    print()
    print(f"credit_metrics: {result_state['credit_metrics']}")
    print()
