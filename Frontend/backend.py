"""Where a question goes.

Two backends, one interface. Locally a question is answered by the LangGraph
workflow in this process; on EC2 it is answered by one Step Functions execution
in the account. `run_query` returns the same shape either way, so the views and
the memo do not know which one ran.

    RISKFORGE_BACKEND=local           the workflow in-process (the default)
    RISKFORGE_BACKEND=stepfunctions   arn:aws:states:...:riskforge-pipeline

The local branch is a single call and always was; the cloud branch is a start,
a poll and an adapter. The adapter exists because the execution output was
designed for the pipeline's own contract -- `answered`, `risk_analysis`,
`compliance` -- while the interface was written against the workflow's state
fields. Renaming either to match the other would have meant editing a deployed
Lambda or 2,300 lines of rendering, so the translation lives here instead, in
one function, where the mapping can be read as a table.

What this module deliberately cannot do: reach the database, hold a model key,
or see a loan. The cloud branch reads named fields out of one JSON document and
the execution output has no per-loan field in it -- riskforge/outputs.py refuses
to write one and the BuildProfile state drops any row execute-sql returned
inline. Nothing here fetches the query result from S3, and the instance role is
not permitted to.
"""
from pathlib import Path
import json
import os
import sys
import time

FRONTEND_DIR = Path(__file__).resolve().parent
if str(FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(FRONTEND_DIR))

MODE = (os.environ.get("RISKFORGE_BACKEND") or "local").strip().lower()
PROJECT = "riskforge"
MACHINE = "%s-pipeline" % PROJECT
REGION = os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
MAX_RETRIES = 3

# Seconds to poll before giving up on the answer. The execution's own timeout is
# var.pipeline_execution_timeout (1800); this is shorter because somebody is
# watching a spinner. Giving up here does not cancel the execution -- it is
# still in the history, and still finishes.
POLL_TIMEOUT = int(os.environ.get("RISKFORGE_POLL_TIMEOUT") or 600)
POLL_INTERVAL = 2.0

# Local mode offers the "run risk analysis on these rows" button because the
# rows are in memory. Cloud mode cannot: the population is in S3 and only the
# Fargate tasks read it, and the guard already decided this question did not
# need scoring. Re-running the pipeline would answer the same question the same
# way, so the button is hidden rather than made to lie.
RISK_ANALYSIS_ON_DEMAND = MODE != "stepfunctions"


def describe() -> str:
    """One line for the sidebar, so which backend answered is never a guess."""
    if MODE == "stepfunctions":
        return "AWS Step Functions -- %s (%s)" % (MACHINE, REGION)
    return "Local LangGraph workflow"


# --- Local: the workflow in this process ---------------------------------


def _run_local(query: str):
    """Imported inside the function on purpose. These three modules pull in
    langgraph, pydantic, xgboost and scikit-learn, which are about 1.5 GB of
    wheels and roughly 30 seconds of import -- and none of it exists on the EC2
    instance, which installs six packages and calls an API."""
    from memory.state import RiskGraphState
    from workflow.graph import graph

    return graph.invoke(RiskGraphState(query=query, max_retries=MAX_RETRIES))


def run_risk_analysis(result_state):
    """The on-demand path, local only. Guarded by RISK_ANALYSIS_ON_DEMAND at the
    call site; this raise is the second line of defence rather than the first."""
    if MODE == "stepfunctions":
        raise RuntimeError(
            "Risk analysis cannot be added to a finished execution: the rows are "
            "in S3 and only the Fargate tasks read them. Ask the question again "
            "in a form that implies risk analysis.")
    from memory.state import RiskGraphState
    from workflow.nodes import (
        credit_risk_agent,
        interest_rate_concentration_agent,
        compliance_agent,
    )

    state = RiskGraphState.model_validate(result_state)
    state = state.model_copy(update={"requires_risk_analysis": True}, deep=False)
    state = state.model_copy(update=credit_risk_agent.run(state), deep=False)
    state = state.model_copy(update=interest_rate_concentration_agent.run(state), deep=False)
    state = state.model_copy(update=compliance_agent.run(state), deep=False)
    return state


# --- Cloud: one Step Functions execution ---------------------------------


def _client():
    import boto3

    return boto3.client("stepfunctions", region_name=REGION)


def _state_machine_arn(sfn) -> str:
    """Built from the caller's own account rather than looked up. list_state_machines
    cannot be scoped to one resource in IAM, so discovering the ARN by name would
    mean granting the instance the right to enumerate every state machine in the
    account in order to find the one it is allowed to start. sts:GetCallerIdentity
    needs no permission at all, and the name is fixed by Terraform."""
    override = os.environ.get("RISKFORGE_STATE_MACHINE_ARN")
    if override:
        return override.strip()
    import boto3

    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    return "arn:aws:states:%s:%s:stateMachine:%s" % (REGION, account, MACHINE)


def _execution_name() -> str:
    """Hex and one dash. PlanRun formats both S3 keys from $$.Execution.Name, so
    this string becomes a key segment under risk-results/ -- no slash, no space,
    and unique per run so two questions never overwrite each other's aggregates."""
    import uuid

    return "ui-%s" % uuid.uuid4().hex[:12]


def _run_stepfunctions(query: str) -> dict:
    sfn = _client()
    execution = sfn.start_execution(
        stateMachineArn=_state_machine_arn(sfn),
        name=_execution_name(),
        input=json.dumps({"query": query}),
    )
    arn = execution["executionArn"]

    deadline = time.time() + POLL_TIMEOUT
    while True:
        status = sfn.describe_execution(executionArn=arn)
        if status["status"] != "RUNNING":
            break
        if time.time() > deadline:
            return _adapt_timeout(query, arn)
        time.sleep(POLL_INTERVAL)

    if status["status"] != "SUCCEEDED":
        return _adapt_failure(query, status)

    state = _adapt(json.loads(status["output"]))
    # Set here rather than in the adapter: the ARN is a property of this run, not
    # a field of the pipeline's answer.
    state["execution_arn"] = arn
    return state


def _adapt(output: dict) -> dict:
    """The execution output, renamed into the fields the views read.

        execution output          state field the interface reads
        ----------------          -------------------------------
        answered                  data_available
        risk_analysis             requires_risk_analysis
        reason                    guard_reason          (refusals only)
        retries                   data_agent_result.retries_used
        compliance                compliance_result
        regulatory_capital        compliance_result.regulatory_capital
        query, sql_query, row_count, truncated, credit_metrics, rate_metrics
                                  same names

    regulatory_capital moves *into* compliance_result because that is where the
    local state put it and where the cards and the memo look for it. In the cloud
    it arrives as a sibling: the credit branch computes RWA, and the compliance
    Lambda deliberately holds no model, so it returns the field as null with a
    note saying so. Grafting it here keeps one reader and one shape.

    Every value below is a scalar or an aggregate. There is no branch that copies
    an unnamed field through, so a per-loan field added upstream later still could
    not reach the interface by this route.
    """
    answered = bool(output.get("answered"))
    compliance = dict(output.get("compliance") or {})
    if output.get("regulatory_capital") is not None:
        compliance["regulatory_capital"] = output["regulatory_capital"]
        # And drop the Lambda's note about not computing it. That note is true of
        # the Lambda and false of the answer -- the credit branch computed RWA and
        # it is grafted in on the line above -- so leaving it would put "not
        # computed here" next to eight figures of it.
        compliance.pop("regulatory_capital_note", None)

    row_count = int(output.get("row_count") or 0)
    retries = int(output.get("retries") or 0)
    sql_query = output.get("sql_query") or ""

    return {
        "query": output.get("query", ""),
        "sql_query": sql_query,
        "source_uri": output.get("source_uri"),
        "row_count": row_count,
        "truncated": bool(output.get("truncated")),
        "data_available": answered,
        "requires_risk_analysis": bool(output.get("risk_analysis")),
        "guard_reason": output.get("reason"),
        "guard_blocked": not answered,
        "credit_metrics": output.get("credit_metrics") or {},
        "rate_metrics": output.get("rate_metrics") or {},
        "compliance_result": compliance,
        "data_agent_result": {
            "success": answered,
            "sql_query": sql_query,
            "row_count": row_count,
            "retries_used": retries,
            "message": output.get("reason") or "",
        },
        "pipeline_error": None,
    }


def _failed(query: str, error: str, cause: str, arn, guard_blocked: bool) -> dict:
    """A failed execution has no output at all -- describe_execution returns an
    error name and a cause and nothing else -- so the state is assembled from
    those two strings.

    The attempt count is not among them, and it is deliberately not fetched:
    reading it would mean pulling the execution history with payloads, and the
    ExecuteSql result in that history carries one inline row. So the trace says
    'no valid result' without a count rather than importing a borrower row into
    this process to print a number. pipeline_error is what tells the trace to
    omit the count instead of showing a wrong one."""
    return {
        "query": query,
        "sql_query": "",
        "source_uri": None,
        "row_count": 0,
        "truncated": False,
        "data_available": False,
        "requires_risk_analysis": True,
        "guard_reason": None,
        "guard_blocked": guard_blocked,
        "credit_metrics": {},
        "rate_metrics": {},
        "compliance_result": {},
        "data_agent_result": {
            "success": False,
            "sql_query": "",
            "row_count": 0,
            "retries_used": 0,
            "message": cause or error,
        },
        "pipeline_error": error,
        "execution_arn": arn,
    }


def _adapt_failure(query: str, status: dict) -> dict:
    error = status.get("error") or status["status"]
    # GuardUnavailable is the one failure where the guard itself did not run, so
    # it is the one where the trace should say the guard blocked.
    return _failed(query, error, status.get("cause") or "",
                   status.get("executionArn"), error == "GuardUnavailable")


def _adapt_timeout(query: str, arn: str) -> dict:
    return _failed(
        query, "StillRunning",
        "The execution was still running after %ds, so the interface stopped "
        "waiting. It was not cancelled -- it is in the execution history and "
        "will finish. Ask again, or read the run in the Step Functions console."
        % POLL_TIMEOUT,
        arn, False)


def run_query(query: str):
    """One question in, one state out. A dict from the cloud, a RiskGraphState
    from the local workflow -- and the views read both through theme.value()."""
    if MODE == "stepfunctions":
        return _run_stepfunctions(query)
    return _run_local(query)
