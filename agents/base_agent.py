from abc import ABC, abstractmethod

from utils.logger import logger

from memory.state import RiskGraphState


class BaseAgent(ABC):
    """
    Minimal base class -- deliberately lighter than a single-LLM-call agent
    pattern, since agents in this project (starting with DataAgent) orchestrate
    multiple tools across several steps rather than making one prompt call
    per run. Concrete agents expose their own per-step methods; this base
    class only guarantees a name and a logger.
    """

    def __init__(self, name: str):

        self.name = name

        self.logger = logger
