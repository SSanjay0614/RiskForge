from tools.sql_executor_tool import SQLExecutorTool


tool = SQLExecutorTool()

# Valid query
result = tool.run("SELECT loan_id, loan_amnt, sub_grade FROM Loans LIMIT 5")
print("-" * 60)
print(f"Success:    {result.success}")
print(f"Row count:  {result.row_count}")
print(f"Columns:    {result.columns}")
if result.rows_df is not None:
    print(result.rows_df)

# Malicious/disallowed query -- should be rejected before ever touching the DB
result2 = tool.run("DELETE FROM Loans WHERE 1=1")
print("-" * 60)
print(f"Success: {result2.success}")
print(f"Error:   {result2.error}")

# Malformed SQL -- should return an error, not crash
result3 = tool.run("SELECT nonexistent_column FROM Loans")
print("-" * 60)
print(f"Success: {result3.success}")
print(f"Error:   {result3.error}")
