from pydantic import BaseModel, Field
from typing import List, Optional, Dict


class ComplianceFlag(BaseModel):

    metric_name: str = ""

    value: float = 0.0

    threshold: float = 0.0

    source: str = ""  # 'internal' or 'basel_iii'

    breached: bool = False

    direction: str = ""  # 'max' (internal policy ceiling) or 'min' (Basel regulatory floor)

    citation: Optional[str] = None


class ComplianceResult(BaseModel):

    flags: List[ComplianceFlag] = Field(default_factory=list)

    any_breach: bool = False

    # Basel III regulatory capital -- always computed when scored loan data
    # is available, not conditional on any breach. Citation is hardcoded,
    # not RAG-retrieved: the formula and exposure class are fixed for this
    # portfolio, so which Basel paragraphs apply never varies query to
    # query (unlike the breach-explanation citations above, which do).
    regulatory_capital: Optional[Dict] = None

    regulatory_capital_citation: str = (
        "Basel III CRE31.16 (IRB approach: risk-weight function for 'Other Retail' "
        "exposures) -- RWA = K x 12.5 x EAD, where correlation R = 0.03 x "
        "(1 - e^(-35xPD))/(1 - e^-35) + 0.16 x [1 - that term], and capital "
        "requirement K = LGD x N[(G(PD) + sqrt(R/(1-R)) x G(0.999)) / sqrt(1-R)] "
        "- PD x LGD (no maturity adjustment for retail exposures, per BIS QIS3 FAQ). "
        "PD floor 0.05% and LGD floor 30% for unsecured 'other retail' exposures "
        "per CRE32.58."
    )
