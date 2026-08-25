import pandas as pd

from tools.sql_evaluator_tool import SQLEvaluatorTool


tool = SQLEvaluatorTool()

# Case 1: empty result -- rejected deterministically, no LLM call needed
empty_df = pd.DataFrame(columns=["loan_id", "loan_amnt"])
result_empty = tool.run(
    query="What is our exposure in Wyoming?",
    sql_query="SELECT loan_id, loan_amnt FROM Loans WHERE addr_state = 'WY'",
    rows_df=empty_df,
)
print("-" * 60)
print("EMPTY RESULT CASE")
print(f"Is valid: {result_empty.is_valid}")
print(f"Feedback: {result_empty.feedback}")

# Case 2: plausible result -- goes through the LLM judgment
good_df = pd.DataFrame({
    "loan_id": ["1001", "1002", "1003"],
    "loan_amnt": [10000, 15000, 8000],
    "addr_state": ["CA", "CA", "CA"],
})
result_good = tool.run(
    query="What are the loan amounts for California loans?",
    sql_query="SELECT loan_id, loan_amnt, addr_state FROM Loans WHERE addr_state = 'CA'",
    rows_df=good_df,
)
print("-" * 60)
print("PLAUSIBLE RESULT CASE")
print(f"Is valid: {result_good.is_valid}")
print(f"Feedback: {result_good.feedback}")

# Case 3: mismatched result -- e.g. query asked for a count, but got raw rows
# with an unrelated column -- should plausibly get flagged by the LLM
mismatched_df = pd.DataFrame({"emp_title": ["Teacher", "Manager", "Nurse"]})
result_mismatched = tool.run(
    query="How many loans do we have in the retail sector?",
    sql_query="SELECT emp_title FROM Borrowers LIMIT 3",
    rows_df=mismatched_df,
)
print("-" * 60)
print("MISMATCHED RESULT CASE")
print(f"Is valid: {result_mismatched.is_valid}")
print(f"Feedback: {result_mismatched.feedback}")
