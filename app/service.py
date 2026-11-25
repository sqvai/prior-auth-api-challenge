# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Use cases. One transaction per request, owned by get_db().

Submit always writes a submission_attempts row, including rejections, so support
can answer "my submission didn't go through." Idempotency-Key makes a retry
return that same row instead of opening a second request.
"""

import hashlib
import json
import logging
import uuid
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auditlog
from app.ai import (
    STATIC_FALLBACK,
    AiOutputRejected,
    DocumentationChecker,
    packet_hash,
    sanitize,
)
from app.clock import Clock
from app.eligibility import active_coverage, active_link, currently_treats, latest_coverage
from app.errors import Result, fail
from app.models import (
    AiBriefing,
    AuthRequest,
    AuthRequestLine,
    AuditEvent,
    Member,
    NotificationOutbox,
    Practice,
    SubmissionAttempt,
    User,
)
from app.policy import (
    ADMIN,
    DENIAL_REASON_CODES,
    REVIEWER,
    SUPPORT,
    TERMINAL,
    can_see_request,
    list_clause,
)
from app.schemas import DecisionIn, Problem, SubmitIn
from app.validation import ProcedureInput, SubmissionFacts, review_submission
from app.visibility import present, present_attempt, sees_clinical_note

log = logging.getLogger("prior_auth")

_REQUEST_STATUSES = frozenset({"submitted", "in_review", "approved", "denied", "partial"})


def submit_request(
    db: Session,
    actor: User,
    payload: SubmitIn,
    idempotency_key: str | None,
    clock: Clock,
    briefing_provider: DocumentationChecker | object,
) -> Result:
    if actor.role != "provider":
        return fail(403, "role_cannot_submit", "Only a provider office can submit a prior authorization.")
    key = (idempotency_key or "").strip()
    if not key:
        return fail(
            400,
            "idempotency_key_required",
            "Send an Idempotency-Key header. A retry with the same key will not create a second request.",
        )
    if len(key) > 200:
        return fail(400, "idempotency_key_invalid", "Idempotency-Key must be 200 characters or fewer.")

    practice = db.get(Practice, actor.practice_id)
    if practice is None:
        return fail(500, "practice_missing", "This office user is not attached to a practice.")
    try:
        today = clock.now(practice.timezone).date()
    except ZoneInfoNotFoundError:
        return fail(500, "practice_timezone_invalid", "This practice has a timezone we can't use. Contact operations.")

    digest = _body_hash(payload)
    existing = _attempt_for(db, practice.id, key)
    if existing is not None:
        return _replay(db, actor, existing, digest)

    member = db.get(Member, payload.member_id)
    coverage_active = False
    linked = False
    coverage = None
    if member is not None:
        linked = active_link(db, member.id, practice.id, payload.date_of_service) is not None
        coverage = active_coverage(db, member.id, payload.date_of_service)
        coverage_active = coverage is not None
        if coverage is None:
            coverage = latest_coverage(db, member.id)

    reviewed = review_submission(
        SubmissionFacts(
            today=today,
            timezone_name=practice.timezone,
            date_of_service=payload.date_of_service,
            diagnosis_code=payload.diagnosis_code,
            member_exists=member is not None,
            linked=linked,
            coverage_active=coverage_active,
            coverage_effective_on=None if coverage is None else coverage.effective_on,
            coverage_terminated_on=None if coverage is None else coverage.terminated_on,
            procedures=tuple(ProcedureInput(item.code, item.units) for item in payload.procedures),
            confirm_high_units=payload.confirm_high_units,
        )
    )
    note = payload.clinical_note.strip() if payload.clinical_note and payload.clinical_note.strip() else None
    request: AuthRequest | None = None
    if not reviewed.problems:
        request = AuthRequest(
            practice_id=practice.id,
            member_id=payload.member_id,
            submitted_by=actor.id,
            diagnosis_code=reviewed.diagnosis_code,
            date_of_service=payload.date_of_service,
            clinical_note=note,
            status="submitted",
        )
    attempt = SubmissionAttempt(
        practice_id=practice.id,
        actor_id=actor.id,
        claimed_member_id=payload.member_id,
        idempotency_key=key,
        body_hash=digest,
        outcome="rejected" if reviewed.problems else "accepted",
        problems=[item.model_dump() for item in reviewed.problems],
    )
    try:
        with db.begin_nested():
            if request is not None:
                db.add(request)
                db.flush()
                for position, procedure in enumerate(reviewed.procedures):
                    request.lines.append(
                        AuthRequestLine(
                            position=position,
                            code=procedure.code,
                            code_system=procedure.code_system,
                            units=procedure.units,
                            units_confirmed=procedure.units_confirmed,
                        )
                    )
                attempt.request_id = request.id
            db.add(attempt)
            db.flush()
    except IntegrityError:
        raced = _attempt_for(db, practice.id, key)
        if raced is None:
            raise
        return _replay(db, actor, raced, digest)

    if reviewed.problems:
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.SUBMISSION_REJECTED,
            attempt_id=attempt.id,
            detail={"problem_codes": [item.code for item in reviewed.problems]},
        )
        return Result(
            422,
            {
                "error": "submission_rejected",
                "message": "Fix every problem below, then resubmit with a new Idempotency-Key.",
                "attempt_id": str(attempt.id),
                "problems": attempt.problems,
            },
        )

    assert request is not None
    auditlog.record(
        db,
        actor_id=actor.id,
        action=auditlog.SUBMISSION_ACCEPTED,
        request_id=request.id,
        attempt_id=attempt.id,
        detail={"line_count": len(request.lines)},
    )
    _write_briefing(db, actor, request, briefing_provider)
    body = present(request, actor, detail=True, conflicted=False)
    body["attempt_id"] = str(attempt.id)
    return Result(201, body)


def list_requests(db: Session, actor: User, status: str | None, limit: int, offset: int) -> Result:
    if status is not None and status not in _REQUEST_STATUSES:
        return fail(422, "status_invalid", "Status must be submitted, in_review, approved, denied, or partial.")
    query = select(AuthRequest).order_by(AuthRequest.created_at.desc()).limit(limit).offset(offset)
    clause = list_clause(actor)
    if clause is not None:
        query = query.where(clause)
    if status is not None:
        query = query.where(AuthRequest.status == status)
    items = []
    for request in db.scalars(query):
        conflicted = _conflicted(db, actor, request)
        items.append(present(request, actor, detail=False, conflicted=conflicted))
    return Result(200, {"requests": items, "limit": limit, "offset": offset})


def get_request(db: Session, actor: User, request_id: uuid.UUID) -> Result:
    request = db.get(AuthRequest, request_id)
    if request is None or not can_see_request(actor, request):
        return _missing()
    conflicted = _conflicted(db, actor, request)
    if sees_clinical_note(actor, detail=True, conflicted=conflicted):
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.RECORD_VIEWED,
            request_id=request.id,
            detail={"includes_clinical_note": True},
        )
    elif conflicted:
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.RECORD_VIEW_RESTRICTED,
            request_id=request.id,
            detail={"reason": "treating_relationship"},
        )
    return Result(200, present(request, actor, detail=True, conflicted=conflicted))


def history(db: Session, actor: User, request_id: uuid.UUID) -> Result:
    request = db.get(AuthRequest, request_id)
    if request is None or not can_see_request(actor, request):
        return _missing()
    if actor.role not in (ADMIN, REVIEWER):
        return fail(403, "role_cannot_read_history", "The audit history is for reviewers and operations admins.")
    if _conflicted(db, actor, request) and request.reviewer_id != actor.id:
        return fail(
            403,
            "treating_conflict",
            "You treat this member, so the chart history stays with another reviewer.",
        )
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.request_id == request.id).order_by(AuditEvent.at, AuditEvent.id)
    ).all()
    return Result(
        200,
        {
            "events": [
                {
                    "id": str(event.id),
                    "action": event.action,
                    "actor_id": str(event.actor_id),
                    "at": event.at.isoformat(),
                    "detail": event.detail,
                }
                for event in events
            ]
        },
    )


def pickup(db: Session, actor: User, request_id: uuid.UUID, clock: Clock) -> Result:
    if actor.role != REVIEWER:
        return fail(403, "role_cannot_review", "Only a reviewer can pick up a request.")
    request = _lock(db, request_id)
    if request is None or not can_see_request(actor, request):
        return _missing()
    if currently_treats(db, actor.id, request.member_id):
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.PICKUP_REFUSED,
            request_id=request.id,
            detail={"reason": "treating_relationship"},
        )
        return fail(
            409,
            "treating_conflict",
            "You treat this member, so you can't review this request. An operations admin can hand it to someone else.",
        )
    if request.status in TERMINAL:
        return fail(409, "decision_final", "This request already has a decision. Appeals are a separate process.")
    if request.status == "in_review" and request.reviewer_id == actor.id:
        return Result(200, present(request, actor, detail=True, conflicted=False))
    # Someone else's in_review row is invisible (404 above). We do not reveal
    # that another reviewer already took it.
    request.status = "in_review"
    request.reviewer_id = actor.id
    request.picked_up_at = clock.now("UTC")
    auditlog.record(db, actor_id=actor.id, action=auditlog.PICKED_UP, request_id=request.id, detail={})
    return Result(200, present(request, actor, detail=True, conflicted=False))


def decide(db: Session, actor: User, request_id: uuid.UUID, payload: DecisionIn, clock: Clock) -> Result:
    if actor.role != REVIEWER:
        return fail(403, "role_cannot_review", "Only the assigned reviewer can decide a request.")
    request = _lock(db, request_id)
    if request is None or not can_see_request(actor, request):
        return _missing()
    if request.status in TERMINAL:
        return fail(409, "decision_final", "This request already has a decision. Appeals are a separate process.")
    if request.status != "in_review" or request.reviewer_id != actor.id:
        return fail(
            409,
            "not_your_request",
            "Pick up the request before you decide it. If someone else has it, ask an admin to reassign it.",
        )
    if currently_treats(db, actor.id, request.member_id):
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.DECISION_REFUSED,
            request_id=request.id,
            detail={"reason": "treating_relationship"},
        )
        return fail(409, "treating_conflict", "You treat this member, so you can't decide this request.")

    problems = _decision_problems(request, payload)
    if problems:
        return Result(
            422,
            {
                "error": "decision_invalid",
                "message": "The decision did not cover the request. Nothing was saved.",
                "problems": [item.model_dump() for item in problems],
            },
        )

    by_id = {line.id: line for line in request.lines}
    for item in payload.lines:
        line = by_id[item.line_id]
        line.decision = item.decision
        line.decided_by = actor.id
        if item.decision == "denied":
            line.denial_reason_code = item.denial_reason_code
            line.denial_reason_text = (item.denial_reason_text or "").strip()
        else:
            line.denial_reason_code = None
            line.denial_reason_text = None
        line.internal_comment = (
            item.internal_comment.strip() if item.internal_comment and item.internal_comment.strip() else None
        )

    outcomes = {line.decision for line in request.lines}
    if outcomes == {"approved"}:
        request.status = "approved"
    elif outcomes == {"denied"}:
        request.status = "denied"
    else:
        request.status = "partial"
    request.decided_by = actor.id
    request.decided_at = clock.now("UTC")
    auditlog.record(
        db,
        actor_id=actor.id,
        action=auditlog.DECISION_RECORDED,
        request_id=request.id,
        detail={
            "status": request.status,
            "lines": [
                {
                    "id": str(line.id),
                    "code": line.code,
                    "decision": line.decision,
                    "denial_reason_code": line.denial_reason_code,
                }
                for line in request.lines
            ],
        },
    )
    db.add(
        NotificationOutbox(
            request_id=request.id,
            practice_id=request.practice_id,
            kind="decision_made",
            payload={
                "request_id": str(request.id),
                "status": request.status,
                "lines": [
                    {
                        "code": line.code,
                        "decision": line.decision,
                        "denial_reason_code": line.denial_reason_code,
                        "denial_reason_text": line.denial_reason_text,
                    }
                    for line in request.lines
                ],
            },
        )
    )
    auditlog.record(
        db,
        actor_id=actor.id,
        action=auditlog.NOTIFICATION_ENQUEUED,
        request_id=request.id,
        detail={"kind": "decision_made"},
    )
    return Result(200, present(request, actor, detail=True, conflicted=False))


def reassign(db: Session, actor: User, request_id: uuid.UUID, reviewer_id: uuid.UUID, clock: Clock) -> Result:
    if actor.role != ADMIN:
        return fail(403, "role_cannot_reassign", "Only an operations admin can hand a request to another reviewer.")
    request = _lock(db, request_id)
    if request is None:
        return _missing()
    if request.status in TERMINAL:
        return fail(409, "decision_final", "This request already has a decision. Appeals are a separate process.")
    reviewer = db.get(User, reviewer_id)
    if reviewer is None or reviewer.role != REVIEWER:
        return fail(422, "reviewer_not_found", "That user is not a reviewer.")
    if currently_treats(db, reviewer.id, request.member_id):
        return fail(409, "treating_conflict", "That reviewer treats this member, so they can't take this request.")
    previous = request.reviewer_id
    request.reviewer_id = reviewer.id
    request.status = "in_review"
    request.picked_up_at = clock.now("UTC")
    auditlog.record(
        db,
        actor_id=actor.id,
        action=auditlog.REASSIGNED,
        request_id=request.id,
        detail={
            "from_reviewer_id": None if previous is None else str(previous),
            "to_reviewer_id": str(reviewer.id),
        },
    )
    return Result(200, present(request, actor, detail=True, conflicted=False))


def list_attempts(
    db: Session,
    actor: User,
    *,
    attempt_id: uuid.UUID | None,
    practice_id: uuid.UUID | None,
    member_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> Result:
    if actor.role not in (ADMIN, SUPPORT):
        return fail(
            403,
            "role_cannot_search_attempts",
            "Submission lookup is for the support desk and operations admins.",
        )
    query = select(SubmissionAttempt).order_by(SubmissionAttempt.created_at.desc()).limit(limit).offset(offset)
    if attempt_id is not None:
        query = query.where(SubmissionAttempt.id == attempt_id)
    if practice_id is not None:
        query = query.where(SubmissionAttempt.practice_id == practice_id)
    if member_id is not None:
        query = query.where(SubmissionAttempt.claimed_member_id == member_id)
    rows = list(db.scalars(query))
    auditlog.record(
        db,
        actor_id=actor.id,
        action=auditlog.SUPPORT_LOOKUP,
        detail={
            "attempt_id": None if attempt_id is None else str(attempt_id),
            "practice_id": None if practice_id is None else str(practice_id),
            "claimed_member_id": None if member_id is None else str(member_id),
            "count": len(rows),
        },
    )
    return Result(200, {"attempts": [present_attempt(row) for row in rows], "limit": limit, "offset": offset})


def reviewer_load(db: Session, actor: User) -> Result:
    if actor.role != ADMIN:
        return fail(403, "role_cannot_view_load", "Reviewer load is for operations admins.")
    reviewers = list(db.scalars(select(User).where(User.role == REVIEWER).order_by(User.display_name)))
    counts = {
        reviewer_id: count
        for reviewer_id, count in db.execute(
            select(AuthRequest.reviewer_id, func.count())
            .where(AuthRequest.status == "in_review")
            .group_by(AuthRequest.reviewer_id)
        ).all()
    }
    return Result(
        200,
        {
            "reviewers": [
                {
                    "reviewer_id": str(user.id),
                    "display_name": user.display_name,
                    "open_requests": int(counts.get(user.id, 0)),
                }
                for user in reviewers
            ]
        },
    )


def list_notifications(db: Session, actor: User) -> Result:
    if actor.role != ADMIN:
        return fail(403, "role_cannot_view_notifications", "The notice outbox is for operations admins.")
    rows = db.scalars(
        select(NotificationOutbox)
        .where(NotificationOutbox.delivered_at.is_(None))
        .order_by(NotificationOutbox.created_at.desc())
        .limit(100)
    ).all()
    return Result(
        200,
        {
            "notifications": [
                {
                    "id": str(row.id),
                    "request_id": str(row.request_id),
                    "practice_id": str(row.practice_id),
                    "kind": row.kind,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
        },
    )


def _write_briefing(db: Session, actor: User, request: AuthRequest, provider: object) -> None:
    packet = {
        "clinical_note": request.clinical_note or "",
        "date_of_service": request.date_of_service.isoformat(),
        "diagnosis_code": request.diagnosis_code,
        "procedures": [{"code": line.code, "units": line.units} for line in request.lines],
        "units_confirmed": any(line.units_confirmed for line in request.lines),
    }
    local = DocumentationChecker()
    attempted = getattr(provider, "name", "unknown")
    output_rejected = False
    author: object | None = provider
    try:
        clean = sanitize(provider.brief(packet))  # type: ignore[attr-defined]
    except AiOutputRejected:
        output_rejected = True
        author, clean = _local_or_static(local, packet)
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.AI_OUTPUT_REJECTED,
            request_id=request.id,
            detail={"attempted_provider": attempted},
        )
        log.info("ai output rejected request_id=%s", request.id)
    except Exception:
        output_rejected = True
        author, clean = _local_or_static(local, packet)
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.AI_BRIEFING_FAILED,
            request_id=request.id,
            detail={"attempted_provider": attempted},
        )
        log.info("ai briefing failed request_id=%s", request.id)
    else:
        auditlog.record(
            db,
            actor_id=actor.id,
            action=auditlog.AI_BRIEFING_CREATED,
            request_id=request.id,
            detail={"provider": attempted},
        )
    if author is None:
        provider_name, model_name = "static-fallback", "static"
    else:
        provider_name, model_name = author.name, author.model  # type: ignore[attr-defined]
    request.briefings.append(
        AiBriefing(
            attempted_provider=attempted,
            provider=provider_name,
            model=model_name,
            input_hash=packet_hash(packet),
            missing_information=list(clean.missing_information),
            extracted_facts=list(clean.extracted_facts),
            narrative=clean.narrative,
            output_rejected=output_rejected,
        )
    )
    db.flush()


def _local_or_static(local: DocumentationChecker, packet: dict):
    try:
        return local, sanitize(local.brief(packet))
    except Exception:
        return None, STATIC_FALLBACK


def _decision_problems(request: AuthRequest, payload: DecisionIn) -> list[Problem]:
    known = {line.id for line in request.lines}
    incoming = [item.line_id for item in payload.lines]
    problems: list[Problem] = []
    if len(incoming) != len(set(incoming)):
        problems.append(
            Problem(
                code="DUPLICATE_LINE",
                field="lines",
                message="Each procedure line can appear only once in the decision.",
            )
        )
    missing = sorted(str(line_id) for line_id in known if line_id not in set(incoming))
    unknown = sorted(str(line_id) for line_id in incoming if line_id not in known)
    if missing or unknown:
        problems.append(
            Problem(
                code="LINES_MUST_MATCH",
                field="lines",
                message=(
                    "Decide every procedure on the request, and no others, in this one submission. "
                    f"Missing line ids: {missing or ['none']}. Unknown line ids: {unknown or ['none']}."
                ),
            )
        )
    for index, item in enumerate(payload.lines):
        field = f"lines[{index}]"
        if item.decision not in ("approved", "denied"):
            problems.append(
                Problem(
                    code="DECISION_INVALID",
                    field=f"{field}.decision",
                    message="Each line must be approved or denied.",
                )
            )
            continue
        has_reason = bool(item.denial_reason_code) or bool(item.denial_reason_text and item.denial_reason_text.strip())
        if item.decision == "approved" and has_reason:
            problems.append(
                Problem(
                    code="APPROVAL_HAS_DENIAL_REASON",
                    field=field,
                    message="An approved line cannot include a denial reason. Remove it.",
                )
            )
        if item.decision == "denied":
            if item.denial_reason_code not in DENIAL_REASON_CODES:
                problems.append(
                    Problem(
                        code="DENIAL_REASON_REQUIRED",
                        field=f"{field}.denial_reason_code",
                        message="A denial needs one of these reason codes: " + ", ".join(sorted(DENIAL_REASON_CODES)) + ".",
                    )
                )
            if not (item.denial_reason_text or "").strip():
                problems.append(
                    Problem(
                        code="DENIAL_REASON_REQUIRED",
                        field=f"{field}.denial_reason_text",
                        message=(
                            "A denial needs a reason the office can read. "
                            "Say what is missing or why the procedure is not covered."
                        ),
                    )
                )
    return problems


def _replay(db: Session, actor: User, existing: SubmissionAttempt, digest: str) -> Result:
    if existing.body_hash != digest:
        return fail(
            409,
            "idempotency_conflict",
            "This Idempotency-Key was already used for a different submission. Send a new key.",
        )
    auditlog.record(
        db,
        actor_id=actor.id,
        action=auditlog.SUBMISSION_REPLAYED,
        request_id=existing.request_id,
        attempt_id=existing.id,
        detail={"outcome": existing.outcome},
    )
    if existing.outcome == "accepted" and existing.request_id is not None:
        request = db.get(AuthRequest, existing.request_id)
        if request is None:
            return fail(500, "attempt_missing_request", "The original request for this key is missing.")
        body = present(request, actor, detail=True, conflicted=False)
        body["attempt_id"] = str(existing.id)
        body["idempotent_replay"] = True
        return Result(200, body)
    return Result(
        422,
        {
            "error": "submission_rejected",
            "message": "This submission was already rejected. Fix the problems and resubmit with a new Idempotency-Key.",
            "attempt_id": str(existing.id),
            "problems": existing.problems,
            "idempotent_replay": True,
        },
    )


def _attempt_for(db: Session, practice_id: uuid.UUID, key: str) -> SubmissionAttempt | None:
    return db.scalar(
        select(SubmissionAttempt).where(
            SubmissionAttempt.practice_id == practice_id,
            SubmissionAttempt.idempotency_key == key,
        )
    )


def _lock(db: Session, request_id: uuid.UUID) -> AuthRequest | None:
    return db.scalar(select(AuthRequest).where(AuthRequest.id == request_id).with_for_update())


def _conflicted(db: Session, actor: User, request: AuthRequest) -> bool:
    return actor.role == REVIEWER and currently_treats(db, actor.id, request.member_id)


def _missing() -> Result:
    return fail(404, "not_found", "No prior authorization request with that id is visible to you.")


def _body_hash(payload: SubmitIn) -> str:
    raw = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
