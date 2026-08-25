from pydantic import BaseModel


class SchemaGuardResult(BaseModel):

    is_answerable: bool = False

    reason: str = ""

    # False for queries answerable directly from retrieved rows (counts,
    # lookups, simple lists) that don't need PD/LGD/EL/concentration/
    # regulatory capital computed on top -- lets the graph skip the whole
    # downstream risk pipeline and just return what the Data Agent already
    # retrieved, rather than burying a simple answer under a full risk report.
    requires_risk_analysis: bool = True