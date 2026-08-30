import re

from llm.ollama_provider import ollama_provider

from tools.base_tool import BaseTool

from dmodels.sql_generation_result import SQLGenerationResult

from Database.schema_description import SCHEMA_DESCRIPTION


SQL_GENERATION_PROMPT = """You are a Text-to-SQL agent that generates READ-ONLY
SQLite queries against the following database schema.

DATABASE SCHEMA:
{schema}

USER QUESTION:
"{query}"

{feedback_block}

Your job is ONLY to identify and retrieve the rows relevant to the user's
question. Aggregation, statistical calculations, and risk calculations are
performed downstream in Python.

Rules for the query:

1. Generate exactly ONE SQLite SELECT statement.

2. Always SELECT every column from both Loans and Borrowers using a full join
   on loan_id, written as JOIN Borrowers USING(loan_id) -- NOT
   "ON Loans.loan_id = Borrowers.loan_id", which duplicates the loan_id
   column in the result set. Never select only a subset of columns.

3. NEVER use aggregate functions such as COUNT, SUM, AVG, MIN, MAX, or similar
   functions.

4. NEVER use GROUP BY.

5. Do not perform aggregation, statistical calculations, or risk calculations
   in SQL. These operations happen downstream in Python.

6. Your only job is to retrieve the correct rows and apply the appropriate
   filtering conditions.

7. Only add WHERE conditions that are clearly implied by the user's question.
   Do not invent filters, assumptions, thresholds, categories, dates, or values.

8. If the question does not specify any filtering condition, return all rows
   without a WHERE clause.

9. Use ONLY tables, columns, and relationships that exist in the provided
   schema. Never invent a table, column, relationship, or value.

10. Use valid SQLite syntax.

11. If the question refers to a field or concept, use the corresponding field
    from the schema. Do not substitute an unrelated field merely because its
    name appears similar.

12. Preserve the meaning of the user's filters exactly. For example, if the
    user asks for California loans, filter for the California value in the
    appropriate state column.

13. If a previous attempt was rejected, use the evaluator feedback to correct
    the query. Fix the specific problem identified without unnecessarily
    changing parts of the query that were already correct.

14. Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA,
    ATTACH, or any other non-SELECT statement.

15. Do not generate multiple SQL statements or use semicolon-separated
    statements.

16. Do not include SQL comments.

17. Do not explain your reasoning.

18. Return ONLY the SQL query. Do not use markdown fences.

The output must be a single SELECT statement that retrieves all columns from
Loans and Borrowers through their loan_id relationship and filters only the
rows relevant to the user's question.
"""




FEEDBACK_BLOCK_TEMPLATE = """
A previous SQL attempt was rejected by the SQL Evaluator.

Evaluator feedback:
"{feedback}"

Generate a corrected query that addresses the specific problem identified in
the feedback while still answering the original question and following all
rules above.
Do not blindly change unrelated parts of the query.
"""




def _clean_sql(text: str) -> str:
 
    text = text.strip()
    text = re.sub(r"^```(sql)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()
    text = text.rstrip(";").strip()
 
    return text
 
 
class TextToSQLTool(BaseTool):
    """
    Generates a single SQL SELECT statement from a natural language query.
    Supports an optional `feedback` argument for retry attempts, so a
    rejected query gets regenerated with the evaluator's specific reason in
    context rather than blindly retried.
 
    Does NOT execute the query -- see SQLExecutorTool. Read-only enforcement
    happens there, independent of whatever this tool produces.
    """
 
    def __init__(self):
        super().__init__("Text to SQL Tool")
        self.llm = ollama_provider.get_llm()
 
    def run(self, query: str, feedback: str = None) -> SQLGenerationResult:
 
        feedback_block = (
            FEEDBACK_BLOCK_TEMPLATE.format(feedback=feedback) if feedback else ""
        )
 
        prompt = SQL_GENERATION_PROMPT.format(
            schema=SCHEMA_DESCRIPTION, query=query, feedback_block=feedback_block
        )
 
        response = self.llm.invoke(prompt)
        sql_query = _clean_sql(response.content)
 
        is_select = bool(re.match(r"(?is)^\s*SELECT\b", sql_query))
 
        return SQLGenerationResult(sql_query=sql_query, is_select=is_select)