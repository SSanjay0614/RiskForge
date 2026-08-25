from tools.schema_guard_tool import SchemaGuardTool


tool = SchemaGuardTool()

test_queries = [
    "What is our total exposure in California?",
    "Which loans have a sub_grade of B3 or worse?",
    "What's the weather like in New York today?",
    "Which specific borrowers defaulted last year?",  # not answerable -- no outcome data
]

for query in test_queries:
    result = tool.run(query)
    print("-" * 60)
    print(f"Query:         {query}")
    print(f"Is answerable: {result.is_answerable}")
    print(f"Reason:        {result.reason}")
