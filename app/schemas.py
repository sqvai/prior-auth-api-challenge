# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class Problem(BaseModel):
    code: str
    field: str | None = None
    message: str


class ProcedureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    units: int


class SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: uuid.UUID
    diagnosis_code: str = Field(min_length=1, max_length=16)
    date_of_service: date
    procedures: list[ProcedureIn] = Field(min_length=1, max_length=20)
    clinical_note: str | None = Field(default=None, max_length=20_000)
    confirm_high_units: bool = False


class LineDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: uuid.UUID
    decision: str
    denial_reason_code: str | None = None
    denial_reason_text: str | None = None
    internal_comment: str | None = Field(default=None, max_length=4_000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[LineDecisionIn] = Field(min_length=1, max_length=20)


class ReassignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_id: uuid.UUID
