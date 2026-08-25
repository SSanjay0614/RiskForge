from memory.state import RiskGraphState


class WorkflowRouter:
    """
    Deterministic routing -- no LLM call. The tool sequence itself has no
    real ambiguity to plan around (SQL can't be evaluated before it's
    executed, etc.); the only genuine decision point is retry-vs-stop after
    evaluation, which is a plain check against retry_count and the
    evaluator's verdict.
    """

    def route_after_guard(self, state: RiskGraphState) -> str:

        return "generate_sql" if state.data_available else "end"

    def route_after_evaluate(self, state: RiskGraphState) -> str:

        if state.evaluator_feedback is None:
            # Valid result -- but only proceed to the full risk pipeline if
            # the query actually needed it. A simple count/list/lookup
            # query already has its answer sitting in rows_df/row_count;
            # running feature engineering, PD/LGD scoring, concentration,
            # and compliance checks on top of it would be wasted work and
            # would bury a simple answer under an irrelevant risk report.
            return "fan_out" if state.requires_risk_analysis else "end"

        if state.retry_count >= state.max_retries:
            return "give_up"  # state.data_available is set by the give_up NODE,
            # not here -- routers only select edges, they don't mutate state
            # (a mutation here silently wouldn't persist; caught via testing)

        return "generate_sql"  # retry, with feedback already set on state