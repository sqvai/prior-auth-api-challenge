# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Submission rules. Each problem tells the office what to change.

Pure functions: the database lookups happen in eligibility.py before this runs.
Collect every problem. Offices should not have to resubmit once per mistake.
"""

import re
from dataclasses import dataclass
from datetime import date

from app.schemas import Problem

# ICD-10-CM shape, not a full code set. We were not given one.
_ICD10 = re.compile(r"^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
_CPT = re.compile(r"^\d{5}$")
_HCPCS = re.compile(r"^[A-Z]\d{4}$")

# Above this, the office must say they meant it. Above MAX, we refuse anyway.
HIGH_UNITS = 30
MAX_UNITS = 200


@dataclass(frozen=True)
class ProcedureInput:
    code: str
    units: int


@dataclass(frozen=True)
class SubmissionFacts:
    today: date
    timezone_name: str
    date_of_service: date
    diagnosis_code: str
    member_exists: bool
    linked: bool
    coverage_active: bool
    coverage_effective_on: date | None
    coverage_terminated_on: date | None
    procedures: tuple[ProcedureInput, ...]
    confirm_high_units: bool


@dataclass(frozen=True)
class NormalizedProcedure:
    code: str
    code_system: str
    units: int
    units_confirmed: bool


@dataclass(frozen=True)
class SubmissionReview:
    diagnosis_code: str
    procedures: tuple[NormalizedProcedure, ...]
    problems: tuple[Problem, ...]


def add_one_year(value: date) -> date:
    """Calendar year, not 365 days. Feb 29 lands on Feb 28 the next year."""
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, day=28)


def review_submission(facts: SubmissionFacts) -> SubmissionReview:
    problems: list[Problem] = []
    problems.extend(_date_window(facts))
    problems.extend(_member(facts))
    if facts.member_exists:
        problems.extend(_coverage(facts))
    diagnosis, diagnosis_problems = _diagnosis(facts.diagnosis_code)
    problems.extend(diagnosis_problems)
    procedures, procedure_problems = _procedures(facts)
    problems.extend(procedure_problems)
    return SubmissionReview(diagnosis, tuple(procedures), tuple(problems))


def _date_window(facts: SubmissionFacts) -> list[Problem]:
    if facts.date_of_service < facts.today:
        return [
            Problem(
                code="DOS_IN_PAST",
                field="date_of_service",
                message=(
                    f"Date of service {facts.date_of_service.isoformat()} is already in the past "
                    f"in {facts.timezone_name}, where today is {facts.today.isoformat()}. "
                    "Retro review is a separate process. Submit today or a future date."
                ),
            )
        ]
    last_accepted = add_one_year(facts.today)
    if facts.date_of_service > last_accepted:
        return [
            Problem(
                code="DOS_TOO_FAR",
                field="date_of_service",
                message=(
                    f"Date of service {facts.date_of_service.isoformat()} is more than a year after "
                    f"today ({facts.today.isoformat()}) in {facts.timezone_name}. "
                    f"The latest date we can accept is {last_accepted.isoformat()}."
                ),
            )
        ]
    return []


def _member(facts: SubmissionFacts) -> list[Problem]:
    if not facts.member_exists:
        return [
            Problem(
                code="MEMBER_UNKNOWN",
                field="member_id",
                message="No member has that id. Check the id from eligibility and resubmit.",
            )
        ]
    if not facts.linked:
        return [
            Problem(
                code="MEMBER_NOT_LINKED",
                field="member_id",
                message=(
                    f"This member is not linked to your practice on {facts.date_of_service.isoformat()}. "
                    "Link the patient to the practice for that date, or submit from the practice that treats them."
                ),
            )
        ]
    return []


def _coverage(facts: SubmissionFacts) -> list[Problem]:
    if facts.coverage_active:
        return []
    if facts.coverage_effective_on is None:
        when = "This member has no coverage on file."
    elif facts.coverage_terminated_on is not None and facts.date_of_service > facts.coverage_terminated_on:
        when = (
            f"Coverage on file ended {facts.coverage_terminated_on.isoformat()} "
            f"(it started {facts.coverage_effective_on.isoformat()})."
        )
    elif facts.date_of_service < facts.coverage_effective_on:
        when = f"Coverage on file does not start until {facts.coverage_effective_on.isoformat()}."
    else:
        when = "Coverage on file is not active on that date."
    return [
        Problem(
            code="COVERAGE_INACTIVE",
            field="date_of_service",
            message=(
                f"The member does not have active coverage on {facts.date_of_service.isoformat()}. "
                f"{when} Update coverage or change the date of service."
            ),
        )
    ]


def _diagnosis(raw: str) -> tuple[str, list[Problem]]:
    code = raw.strip().upper()
    if _ICD10.fullmatch(code):
        return code, []
    return code, [
        Problem(
            code="DIAGNOSIS_INVALID",
            field="diagnosis_code",
            message=(
                f"Diagnosis {raw.strip()!r} is not an ICD-10-CM code. "
                "Use a code like M17.11 (a letter, two characters, then an optional decimal)."
            ),
        )
    ]


def _procedures(facts: SubmissionFacts) -> tuple[list[NormalizedProcedure], list[Problem]]:
    problems: list[Problem] = []
    normalized: list[NormalizedProcedure] = []
    seen: dict[str, int] = {}
    for index, procedure in enumerate(facts.procedures):
        field = f"procedures[{index}]"
        code = procedure.code.strip().upper()
        system = _code_system(code)
        if system is None:
            problems.append(
                Problem(
                    code="PROCEDURE_CODE_INVALID",
                    field=f"{field}.code",
                    message=(
                        f"Procedure {procedure.code.strip()!r} is not a CPT code (5 digits) or a "
                        "HCPCS code (one letter and four digits). Put modifiers in the clinical note."
                    ),
                )
            )
            continue
        if code in seen:
            problems.append(
                Problem(
                    code="DUPLICATE_PROCEDURE",
                    field=f"{field}.code",
                    message=(
                        f"Procedure {code} is listed more than once (also at procedures[{seen[code]}]). "
                        "List each code once and put the count in units."
                    ),
                )
            )
            continue
        seen[code] = index
        unit_problem, confirmed = _units(field, code, procedure.units, facts.confirm_high_units)
        if unit_problem is not None:
            problems.append(unit_problem)
            continue
        normalized.append(
            NormalizedProcedure(code=code, code_system=system, units=procedure.units, units_confirmed=confirmed)
        )
    return normalized, problems


def _code_system(code: str) -> str | None:
    if _CPT.fullmatch(code):
        return "CPT"
    if _HCPCS.fullmatch(code):
        return "HCPCS"
    return None


def _units(field: str, code: str, units: int, confirmed: bool) -> tuple[Problem | None, bool]:
    if units < 1 or units > MAX_UNITS:
        return (
            Problem(
                code="UNITS_OUT_OF_RANGE",
                field=f"{field}.units",
                message=(
                    f"Procedure {code} has {units} units. Send a whole number from 1 to {MAX_UNITS}. "
                    "Split the request or contact operations if you truly need more."
                ),
            ),
            False,
        )
    if units > HIGH_UNITS and not confirmed:
        return (
            Problem(
                code="UNITS_LIKELY_TYPO",
                field=f"{field}.units",
                message=(
                    f"Procedure {code} has {units} units. More than {HIGH_UNITS} is almost always a typo. "
                    "Resubmit the right number, or send confirm_high_units true if this count is intentional."
                ),
            ),
            False,
        )
    return None, units > HIGH_UNITS
