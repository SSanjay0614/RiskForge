from typing import Dict, Any

from agents.data_agent import DataAgent
from agents.credit_risk_agent import CreditRiskAgent
from agents.interest_rate_concentration_agent import InterestRateConcentrationAgent
from agents.compliance_agent import ComplianceAgent

from memory.state import RiskGraphState


data_agent = DataAgent()
credit_risk_agent = CreditRiskAgent()
interest_rate_concentration_agent = InterestRateConcentrationAgent()
compliance_agent = ComplianceAgent()


def guard_node(state: RiskGraphState) -> Dict[str, Any]:
    return data_agent.guard(state)


def generate_sql_node(state: RiskGraphState) -> Dict[str, Any]:
    return data_agent.generate_sql(state)


def execute_sql_node(state: RiskGraphState) -> Dict[str, Any]:
    return data_agent.execute_sql(state)


def evaluate_node(state: RiskGraphState) -> Dict[str, Any]:
    return data_agent.evaluate(state)


def give_up_node(state: RiskGraphState) -> Dict[str, Any]:
    return data_agent.give_up(state)


def credit_risk_node(state: RiskGraphState) -> Dict[str, Any]:
    return credit_risk_agent.run(state)


def interest_rate_concentration_node(state: RiskGraphState) -> Dict[str, Any]:
    return interest_rate_concentration_agent.run(state)


def compliance_node(state: RiskGraphState) -> Dict[str, Any]:
    return compliance_agent.run(state)


def fan_out_node(state: RiskGraphState) -> Dict[str, Any]:
    # Trivial passthrough -- exists only so LangGraph can fan out to two
    # parallel unconditional edges after a conditional decision. Conditional
    # edges map one branch to exactly one destination; genuine parallelism
    # needs an intermediate node with multiple outgoing add_edge calls.
    return {}
