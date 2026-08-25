from tools.text_to_sql_tool import TextToSQLTool


tool = TextToSQLTool()

result = tool.run("What is our total outstanding balance for loans in California?")
print("-" * 60)
print(f"SQL:       {result.sql_query}")
print(f"Is SELECT: {result.is_select}")

# Simulate a retry with evaluator feedback
result_retry = tool.run(
    "What is our total outstanding balance for loans in California?",
    feedback="The query used 'state' as a column name, but the correct column is 'addr_state'.",
)
print("-" * 60)
print(f"SQL (after feedback): {result_retry.sql_query}")
print(f"Is SELECT:            {result_retry.is_select}")
