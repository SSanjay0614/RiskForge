from pydantic import BaseModel


class SQLGenerationResult(BaseModel):

    sql_query: str = ""

    # True only if the generated text actually parses as a SELECT statement.
    # This is a fast pre-check, not the real security boundary -- SQLExecutorTool
    # enforces read-only access independently regardless of this flag.
    is_select: bool = False
