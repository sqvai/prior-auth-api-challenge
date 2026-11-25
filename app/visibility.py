# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Response shaping. Minimum necessary, by role.

Members see the outcome of each procedure. They never see the clinical note,
the denial reason, or the documentation briefing. Those are reviewer reasoning
or chart text. Offices see the denial reason because they have to act on it.
The internal comment stays with the reviewer and operations admins.
Lists are summaries: the note is only on the detail resource, and reading it
is audited in the service.
"""

from app.models import AiBriefing, AuthRequest, AuthRequestLine, User
from app.policy import ADMIN, PROVIDER, REVIEWER


def present(request: AuthRequest, actor: User, *, detail: bool, conflicted: bool) -> dict:
    body = {
        "id": str(request.id),
        "status": request.status,
        "member_id": str(request.member_id),
        "practice_id": str(request.practice_id),
        "diagnosis_code": request.diagnosis_code,
        "date_of_service": request.date_of_service.isoformat(),
        "submitted_at": request.created_at.isoformat(),
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "lines": [_line(line, actor, detail=detail, conflicted=conflicted) for line in request.lines],
    }
    if actor.role in (PROVIDER, REVIEWER, ADMIN) and not (actor.role == REVIEWER and conflicted):
        body["reviewer_id"] = str(request.reviewer_id) if request.reviewer_id else None
    if _sees_note(actor, detail=detail, conflicted=conflicted):
        body["clinical_note"] = request.clinical_note
    if _sees_briefing(actor, detail=detail, conflicted=conflicted) and request.briefings:
        body["documentation_briefing"] = _briefing(request.briefings[-1])
    if actor.role == REVIEWER and conflicted:
        body["treating_conflict"] = True
    return body


def sees_clinical_note(actor: User, *, detail: bool, conflicted: bool) -> bool:
    return _sees_note(actor, detail=detail, conflicted=conflicted)


def _line(line: AuthRequestLine, actor: User, *, detail: bool, conflicted: bool) -> dict:
    row = {
        "id": str(line.id),
        "code": line.code,
        "code_system": line.code_system,
        "units": line.units,
        "decision": line.decision,
    }
    if _sees_denial_reason(actor, conflicted):
        row["units_confirmed"] = line.units_confirmed
        row["denial_reason_code"] = line.denial_reason_code
        row["denial_reason_text"] = line.denial_reason_text
    if detail and _sees_internal(actor, conflicted):
        row["internal_comment"] = line.internal_comment
    return row


def _sees_denial_reason(actor: User, conflicted: bool) -> bool:
    if actor.role in (PROVIDER, ADMIN):
        return True
    return actor.role == REVIEWER and not conflicted


def _sees_internal(actor: User, conflicted: bool) -> bool:
    return actor.role in (REVIEWER, ADMIN) and not conflicted


def _sees_note(actor: User, *, detail: bool, conflicted: bool) -> bool:
    if not detail or conflicted:
        return False
    return actor.role in (PROVIDER, REVIEWER, ADMIN)


def _sees_briefing(actor: User, *, detail: bool, conflicted: bool) -> bool:
    if not detail or conflicted:
        return False
    return actor.role in (REVIEWER, ADMIN)


def _briefing(briefing: AiBriefing) -> dict:
    return {
        "advisory_only": True,
        "attempted_provider": briefing.attempted_provider,
        "provider": briefing.provider,
        "model": briefing.model,
        "output_rejected": briefing.output_rejected,
        "missing_information": list(briefing.missing_information),
        "extracted_facts": list(briefing.extracted_facts),
        "narrative": briefing.narrative,
    }


def present_attempt(attempt) -> dict:
    return {
        "id": str(attempt.id),
        "practice_id": str(attempt.practice_id),
        "actor_id": str(attempt.actor_id),
        "claimed_member_id": str(attempt.claimed_member_id),
        "idempotency_key": attempt.idempotency_key,
        "outcome": attempt.outcome,
        "problems": attempt.problems,
        "request_id": str(attempt.request_id) if attempt.request_id else None,
        "created_at": attempt.created_at.isoformat(),
    }


