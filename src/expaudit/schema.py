"""What the model may say (both paths). pydantic with extra="forbid".

The model output has NO dollars out of policy, approver, action, status or difference type:
any extra field is a validation error. Python computes all of those in decide.py.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MODEL_CODES = ("MISCATEGORIZED", "MEAL_OVER_LIMIT", "ALCOHOL", "LODGING_OVER_LIMIT",
               "PERSONAL_ITEM", "AIRFARE_CLASS")


class ModelFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["MISCATEGORIZED", "MEAL_OVER_LIMIT", "ALCOHOL", "LODGING_OVER_LIMIT",
                  "PERSONAL_ITEM", "AIRFARE_CLASS"]
    cited_clause_ids: list[str] = Field(min_length=1)
    policy_quote: str = Field(min_length=1)
    limit_amount: Decimal | None = None
    memo_quote: str | None = None
    memo_amount: Decimal | None = None
    attendees_count: int | None = None
    flight_hours: Decimal | None = None


class ModelAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ModelFinding]
    explanation: str
    # Client meal (policy 4.3 / 4.4). Reported for the whole line, even with no findings.
    # Python checks both quotes against the memo and decides (decide.py).
    client_meal: bool | None = None
    client_meal_quote: str | None = None
    attendee_names_quote: str | None = None


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_answer(text: str) -> ModelAnswer:
    """Model text -> ModelAnswer. Raises ValueError (pydantic ValidationError is one)."""
    body = _FENCE.sub("", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in the answer")
    return ModelAnswer.model_validate_json(body[start:end + 1])
