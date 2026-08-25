from pydantic import BaseModel


class DataAgentResult(BaseModel):
    """
    Consolidated outcome of a full DataAgent run, set at every terminal path
    (guard rejection, successful evaluation, or give-up after exhausting
    retries) -- one place downstream agents check, rather than needing to
    know which combination of scattered state fields means what.
    """

    success: bool = False

    sql_query: str = ""

    row_count: int = 0

    retries_used: int = 0

    message: str = ""
