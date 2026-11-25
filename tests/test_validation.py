# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

from datetime import date

from app.ai import STATIC_FALLBACK, Briefing, DocumentationChecker, sanitize, AiOutputRejected
from app.validation import ProcedureInput, SubmissionFacts, add_one_year, review_submission


def _facts(**overrides) -> SubmissionFacts:
    base = dict(
        today=date(2026, 11, 24),
        timezone_name="America/New_York",
        date_of_service=date(2026, 12, 24),
        diagnosis_code="m17.11",
        member_exists=True,
        linked=True,
        coverage_active=True,
        coverage_effective_on=date(2020, 1, 1),
        coverage_terminated_on=None,
        procedures=(ProcedureInput("27447", 1),),
        confirm_high_units=False,
    )
    base.update(overrides)
    return SubmissionFacts(**base)


def test_leap_day_lands_on_february_28():
    assert add_one_year(date(2024, 2, 29)) == date(2025, 2, 28)


def test_one_calendar_year_is_accepted_and_a_day_past_it_is_not():
    allowed = review_submission(_facts(date_of_service=date(2027, 11, 24)))
    refused = review_submission(_facts(date_of_service=date(2027, 11, 25)))
    assert allowed.problems == ()
    assert [item.code for item in refused.problems] == ["DOS_TOO_FAR"]
    assert "2027-11-24" in refused.problems[0].message


def test_past_date_names_the_practice_timezone():
    refused = review_submission(_facts(date_of_service=date(2026, 11, 23)))
    assert refused.problems[0].code == "DOS_IN_PAST"
    assert "America/New_York" in refused.problems[0].message
    assert "2026-11-24" in refused.problems[0].message


def test_high_units_need_an_explicit_confirmation():
    typo = review_submission(_facts(procedures=(ProcedureInput("27447", 31),)))
    confirmed = review_submission(_facts(procedures=(ProcedureInput("27447", 31),), confirm_high_units=True))
    absurd = review_submission(_facts(procedures=(ProcedureInput("27447", 201),), confirm_high_units=True))
    assert typo.problems[0].code == "UNITS_LIKELY_TYPO"
    assert "confirm_high_units" in typo.problems[0].message
    assert confirmed.problems == ()
    assert confirmed.procedures[0].units_confirmed is True
    assert absurd.problems[0].code == "UNITS_OUT_OF_RANGE"


def test_thirty_units_do_not_need_confirmation():
    reviewed = review_submission(_facts(procedures=(ProcedureInput("27447", 30),)))
    assert reviewed.problems == ()
    assert reviewed.procedures[0].units_confirmed is False


def test_hcpcs_codes_are_normalized():
    reviewed = review_submission(_facts(procedures=(ProcedureInput("a0428", 1),)))
    assert reviewed.problems == ()
    assert reviewed.procedures[0].code == "A0428"
    assert reviewed.procedures[0].code_system == "HCPCS"


def test_zero_units_are_out_of_range():
    reviewed = review_submission(_facts(procedures=(ProcedureInput("27447", 0),)))
    assert reviewed.problems[0].code == "UNITS_OUT_OF_RANGE"


def test_coverage_messages_distinguish_missing_future_and_inactive():
    missing = review_submission(_facts(coverage_active=False, coverage_effective_on=None, coverage_terminated_on=None))
    future = review_submission(
        _facts(
            coverage_active=False,
            coverage_effective_on=date(2026, 12, 25),
            coverage_terminated_on=None,
            date_of_service=date(2026, 12, 24),
        )
    )
    odd = review_submission(
        _facts(
            coverage_active=False,
            coverage_effective_on=date(2020, 1, 1),
            coverage_terminated_on=date(2027, 1, 1),
        )
    )
    assert "no coverage on file" in missing.problems[0].message
    assert "does not start until 2026-12-25" in future.problems[0].message
    assert "not active on that date" in odd.problems[0].message


def test_inactive_coverage_message_includes_the_end_date():
    reviewed = review_submission(
        _facts(coverage_active=False, coverage_terminated_on=date(2026, 1, 15), date_of_service=date(2026, 12, 24))
    )
    assert reviewed.problems[0].code == "COVERAGE_INACTIVE"
    assert "2026-01-15" in reviewed.problems[0].message


def test_collects_every_problem_at_once():
    reviewed = review_submission(
        _facts(
            member_exists=False,
            diagnosis_code="nope",
            procedures=(ProcedureInput("NOPE", 0), ProcedureInput("27447", 40)),
        )
    )
    codes = {item.code for item in reviewed.problems}
    assert {"MEMBER_UNKNOWN", "DIAGNOSIS_INVALID", "PROCEDURE_CODE_INVALID", "UNITS_LIKELY_TYPO"} <= codes


def test_documentation_checker_does_not_decide():
    briefing = DocumentationChecker().brief(
        {
            "clinical_note": "",
            "date_of_service": "2026-12-24",
            "diagnosis_code": "M17.11",
            "procedures": [{"code": "27447", "units": 1}],
            "units_confirmed": False,
        }
    )
    sanitize(briefing)
    assert "M17.11" in briefing.extracted_facts[1]
    assert "not attached" in briefing.narrative


def test_decision_language_is_discarded():
    try:
        sanitize(Briefing((), (), "APPROVE this procedure"))
    except AiOutputRejected:
        pass
    else:
        raise AssertionError("decision language was kept")
    sanitize(STATIC_FALLBACK)
