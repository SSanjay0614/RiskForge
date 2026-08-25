from typing import Dict, Any

from tools.schema_guard_tool import SchemaGuardTool
from tools.text_to_sql_tool import TextToSQLTool
from tools.sql_executor_tool import SQLExecutorTool
from tools.sql_evaluator_tool import SQLEvaluatorTool

from memory.state import RiskGraphState

from dmodels.data_agent_result import DataAgentResult

from utils.logger import logger


class DataAgent:
    """
    Retrieves loan portfolio data for a natural language query, via a
    schema-relevance guard followed by a generate -> execute -> evaluate
    loop with bounded retries. Each step is exposed as a separate method so
    workflow/nodes.py can wire them as individual LangGraph nodes -- the
    retry loop itself lives in the graph's conditional edges (see
    workflow/router.py), not as a Python while-loop inside this class.

    Each method returns a PARTIAL update dict (only the fields it actually
    changes), not the full state object. This matters once any branch of the
    graph runs in parallel with another (see Credit Risk / Interest Rate
    agents) -- LangGraph treats every field in a returned full state object
    as a write, and two parallel branches both "writing" unchanged fields
    like `query` causes an unresolvable concurrent-write error. Partial
    dicts avoid this entirely, and are the standard LangGraph pattern anyway.

    Tool instances are injectable for testing (fakes/mocks), defaulting to
    the real tools otherwise.
    """

    def __init__(
        self,
        guard_tool: SchemaGuardTool = None,
        sql_tool: TextToSQLTool = None,
        executor_tool: SQLExecutorTool = None,
        evaluator_tool: SQLEvaluatorTool = None,
    ):
        self.name = "Data Agent"

        self.logger = logger

        self.guard_tool = guard_tool or SchemaGuardTool()
        self.sql_tool = sql_tool or TextToSQLTool()
        self.executor_tool = executor_tool or SQLExecutorTool()
        self.evaluator_tool = evaluator_tool or SQLEvaluatorTool()

    def guard(self, state: RiskGraphState) -> Dict[str, Any]:

        result = self.guard_tool.run(state.query)

        updates: Dict[str, Any] = {
            "data_available": result.is_answerable,
            "requires_risk_analysis": result.requires_risk_analysis,
            "guard_reason": result.reason,
        }

        if not result.is_answerable:
            updates["data_agent_result"] = DataAgentResult(
                success=False,
                retries_used=0,
                message=f"Query not answerable against this schema: {result.reason}",
            )

        self.logger.info(f"{self.name} | guard | answerable={result.is_answerable}")

        return updates

    def generate_sql(self, state: RiskGraphState) -> Dict[str, Any]:

        result = self.sql_tool.run(state.query, feedback=state.evaluator_feedback)

        self.logger.info(
            f"{self.name} | generate_sql | attempt={state.retry_count} | sql={result.sql_query!r}"
        )

        return {"sql_query": result.sql_query}

    def execute_sql(self, state: RiskGraphState) -> Dict[str, Any]:

        result = self.executor_tool.run(state.sql_query)

        if not result.success:
            # Execution failure (bad syntax, unknown column) is treated the
            # same as an invalid result -- the DB error becomes retry
            # feedback rather than crashing the graph.
            self.logger.warning(f"{self.name} | execute_sql | failed | {result.error}")

            return {
                "rows_df": None,
                "row_count": 0,
                "evaluator_feedback": result.error,
                "retry_count": state.retry_count + 1,
            }

        rows_df = result.rows_df

        # Add exposure_at_default as a non-destructive alias of
        # outstanding_balance -- FeatureEngineeringTool still needs the raw
        # schema name, but every other downstream consumer (RepricingGapTool,
        # ConcentrationTool, ExpectedLossTool) expects this name. Adding it
        # once here avoids each agent re-deriving it independently.
        if rows_df is not None and "outstanding_balance" in rows_df.columns:
            rows_df = rows_df.copy()
            rows_df["exposure_at_default"] = rows_df["outstanding_balance"]

        if result.truncated:
            self.logger.warning(
                f"{self.name} | execute_sql | result truncated at {result.row_count} rows "
                f"-- query may be broader than intended"
            )

        return {
            "rows_df": rows_df,
            "row_count": result.row_count,
            "truncated": result.truncated,
        }

    def evaluate(self, state: RiskGraphState) -> Dict[str, Any]:

        # execute_sql already failed and incremented retry_count -- nothing
        # to evaluate.
        if state.rows_df is None:
            return {}

        result = self.evaluator_tool.run(state.query, state.sql_query, state.rows_df)

        if result.is_valid:
            self.logger.info(f"{self.name} | evaluate | valid=True | retry_count={state.retry_count}")

            return {
                "evaluator_feedback": None,
                "data_agent_result": DataAgentResult(
                    success=True,
                    sql_query=state.sql_query,
                    row_count=state.row_count,
                    retries_used=state.retry_count,
                    message="Query resolved successfully.",
                ),
            }

        new_retry_count = state.retry_count + 1
        self.logger.info(f"{self.name} | evaluate | valid=False | retry_count={new_retry_count}")

        return {
            "evaluator_feedback": result.feedback,
            "retry_count": new_retry_count,
        }

    def give_up(self, state: RiskGraphState) -> Dict[str, Any]:

        self.logger.warning(
            f"{self.name} | give_up | retries exhausted for query={state.query!r}"
        )

        return {
            "data_available": False,
            "data_agent_result": DataAgentResult(
                success=False,
                sql_query=state.sql_query,
                retries_used=state.retry_count,
                message=f"Gave up after {state.retry_count} retries. Last feedback: {state.evaluator_feedback}",
            ),
        }