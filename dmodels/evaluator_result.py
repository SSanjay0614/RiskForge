from pydantic import BaseModel


class EvaluatorResult(BaseModel):

    is_valid: bool = False

    feedback: str = ""
