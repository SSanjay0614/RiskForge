from memory.state import RiskGraphState

from workflow.graph import graph


test_queries = [
    "What is our total outstanding balance for loans in California?",
    "How many loans do we have with a sub_grade of B3?",
    "What is the average interest rate for debt consolidation loans?",
    "What's the weather like today?",  # should be rejected by the guard
]

for query in test_queries:

    print("=" * 70)
    print(f"QUERY: {query}")
    print("=" * 70)

    result_state = graph.invoke(RiskGraphState(query=query, max_retries=3))

    print(f"data_available: {result_state['data_available']}")
    print(f"guard_reason:   {result_state['guard_reason']}")
    print(f"sql_query:      {result_state['sql_query']}")
    print(f"row_count:      {result_state['row_count']}")
    print(f"retry_count:    {result_state['retry_count']}")

    if result_state.get("data_agent_result"):
        r = result_state["data_agent_result"]
        print(f"data_agent_result.success:      {r.success}")
        print(f"data_agent_result.retries_used: {r.retries_used}")
        print(f"data_agent_result.message:      {r.message}")

    rows_df = result_state.get("rows_df")
    if rows_df is not None:
        print(rows_df.head())

    print()
