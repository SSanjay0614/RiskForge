from langgraph.graph import (
    StateGraph,
    START,
    END
)

from memory.state import RiskGraphState

from workflow.router import WorkflowRouter

from workflow.nodes import (
    guard_node,
    generate_sql_node,
    execute_sql_node,
    evaluate_node,
    give_up_node,
    credit_risk_node,
    interest_rate_concentration_node,
    fan_out_node,
    compliance_node
)


router = WorkflowRouter()

builder = StateGraph(
    RiskGraphState
)


# -------------------------------------------------
# Nodes
# -------------------------------------------------

builder.add_node(
    "guard",
    guard_node
)

builder.add_node(
    "generate_sql",
    generate_sql_node
)

builder.add_node(
    "execute_sql",
    execute_sql_node
)

builder.add_node(
    "evaluate",
    evaluate_node
)

builder.add_node(
    "give_up",
    give_up_node
)

builder.add_node(
    "credit_risk",
    credit_risk_node
)

builder.add_node(
    "interest_rate_concentration",
    interest_rate_concentration_node
)

builder.add_node(
    "fan_out",
    fan_out_node
)

builder.add_node(
    "compliance",
    compliance_node
)


# -------------------------------------------------
# Entry
# -------------------------------------------------

builder.add_edge(
    START,
    "guard"
)


# -------------------------------------------------
# Conditional Routing
# -------------------------------------------------

builder.add_conditional_edges(

    "guard",

    router.route_after_guard,

    {

        "generate_sql": "generate_sql",

        "end": END

    }

)

builder.add_edge(
    "generate_sql",
    "execute_sql"
)

builder.add_edge(
    "execute_sql",
    "evaluate"
)

builder.add_conditional_edges(

    "evaluate",

    router.route_after_evaluate,

    {

        "generate_sql": "generate_sql",

        "give_up": "give_up",

        "fan_out": "fan_out",

        "end": END

    }

)

builder.add_edge(
    "give_up",
    END
)

builder.add_edge(
    "fan_out",
    "credit_risk"
)

builder.add_edge(
    "fan_out",
    "interest_rate_concentration"
)

builder.add_edge(
    "credit_risk",
    "compliance"
)

builder.add_edge(
    "interest_rate_concentration",
    "compliance"
)

builder.add_edge(
    "compliance",
    END
)


# -------------------------------------------------
# Compile Graph
# -------------------------------------------------

graph = builder.compile()