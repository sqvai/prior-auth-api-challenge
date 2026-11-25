# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.ai import Briefing
from app.api import get_briefing_provider, get_clock
from app.clock import FrozenClock
from app.db import SessionLocal
from app.main import app
from app.models import AuthRequest, AuthRequestLine, SubmissionAttempt
from app.seed import (
    MEMBER_ALEX,
    MEMBER_BLAIR,
    MEMBER_CASEY,
    PRACTICE_HI,
    PRACTICE_ME,
    USER_ADMIN,
    USER_ALEX,
    USER_OFFICE_HI,
    USER_OFFICE_ME,
    USER_REVIEWER,
    USER_REVIEWER_2,
    USER_SUPPORT,
    USER_TREATING,
    token_for,
)

NOTE = "ZEBRA-CLINICAL-NOTE Osteoarthritis M17.11. Plan 27447 and 99213."
REASON = "ZEBRA-REVIEWER-REASON Conservative care is not described."
INTERNAL = "ZEBRA-INTERNAL-COMMENT Peer discussion stayed off the letter."


def _tokens() -> dict[str, str]:
    pairs = {
        "office": (USER_OFFICE_ME, "provider"),
        "pacific": (USER_OFFICE_HI, "provider"),
        "alex": (USER_ALEX, "member"),
        "reviewer": (USER_REVIEWER, "reviewer"),
        "treating": (USER_TREATING, "reviewer"),
        "reviewer2": (USER_REVIEWER_2, "reviewer"),
        "admin": (USER_ADMIN, "admin"),
        "support": (USER_SUPPORT, "support"),
    }
    return {name: token_for(user_id, role, ttl_seconds=3600) for name, (user_id, role) in pairs.items()}


def _auth(token: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _body(**overrides) -> dict:
    body = {
        "member_id": str(MEMBER_ALEX),
        "diagnosis_code": "m17.11",
        "date_of_service": "2026-12-24",
        "procedures": [{"code": "27447", "units": 1}, {"code": "99213", "units": 1}],
        "clinical_note": NOTE,
        "confirm_high_units": False,
    }
    body.update(overrides)
    return body


def _submit(client, token: str, body: dict | None = None, key: str | None = None):
    return client.post("/v1/auth-requests", headers=_auth(token, key or str(uuid.uuid4())), json=body or _body())


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_and_bad_tokens_are_rejected(client):
    assert client.post("/v1/auth-requests", json=_body()).status_code == 401
    forged = client.post("/v1/auth-requests", headers=_auth("v1.nope.nope"), json=_body())
    assert forged.status_code == 401
    assert forged.json()["error"] == "unauthorized"


def test_office_must_send_an_idempotency_key(client):
    tokens = _tokens()
    response = client.post("/v1/auth-requests", headers=_auth(tokens["office"]), json=_body())
    assert response.status_code == 400
    assert response.json()["error"] == "idempotency_key_required"


def test_rejected_submission_says_what_to_fix_and_support_can_find_it(client):
    tokens = _tokens()
    unknown = str(uuid.uuid4())
    response = _submit(
        client,
        tokens["office"],
        _body(
            member_id=unknown,
            diagnosis_code="nope",
            date_of_service="2026-11-23",
            procedures=[{"code": "27447", "units": 40}, {"code": "27447", "units": 1}],
        ),
    )
    assert response.status_code == 422, response.text
    body = response.json()
    codes = {item["code"] for item in body["problems"]}
    assert {"DOS_IN_PAST", "MEMBER_UNKNOWN", "DIAGNOSIS_INVALID", "UNITS_LIKELY_TYPO", "DUPLICATE_PROCEDURE"} <= codes
    for problem in body["problems"]:
        assert problem["message"]
        assert "ZEBRA" not in problem["message"]

    found = client.get("/v1/support/attempts", headers=_auth(tokens["support"]), params={"member_id": unknown})
    assert found.status_code == 200
    attempt = found.json()["attempts"][0]
    assert attempt["outcome"] == "rejected"
    assert attempt["id"] == body["attempt_id"]
    assert "clinical_note" not in attempt


def test_idempotency_replays_the_same_request_and_conflicts_on_a_different_body(client):
    tokens = _tokens()
    key = "office-retry-1"
    created = _submit(client, tokens["office"], key=key)
    assert created.status_code == 201, created.text
    again = _submit(client, tokens["office"], key=key)
    assert again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert again.json()["id"] == created.json()["id"]
    changed = _submit(client, tokens["office"], _body(clinical_note="A different note M17.11 27447 99213."), key=key)
    assert changed.status_code == 409
    assert changed.json()["error"] == "idempotency_conflict"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AuthRequest)) == 1
        assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 1


def test_lapsed_coverage_and_unlinked_member(client):
    tokens = _tokens()
    lapsed = _submit(client, tokens["office"], _body(member_id=str(MEMBER_BLAIR)))
    assert lapsed.status_code == 422
    assert any(item["code"] == "COVERAGE_INACTIVE" and "2026-01-15" in item["message"] for item in lapsed.json()["problems"])
    wrong_office = _submit(client, tokens["office"], _body(member_id=str(MEMBER_CASEY)))
    assert any(item["code"] == "MEMBER_NOT_LINKED" for item in wrong_office.json()["problems"])


def test_hawaii_is_still_on_the_previous_day(client):
    tokens = _tokens()
    app.dependency_overrides[get_clock] = lambda: FrozenClock(datetime(2026, 11, 25, 5, 30, tzinfo=timezone.utc))
    try:
        maine = _submit(
            client,
            tokens["office"],
            _body(date_of_service="2026-11-24", procedures=[{"code": "27447", "units": 1}], clinical_note="M17.11 27447"),
        )
        hawaii = _submit(
            client,
            tokens["pacific"],
            _body(
                member_id=str(MEMBER_CASEY),
                date_of_service="2026-11-24",
                procedures=[{"code": "27447", "units": 1}],
                clinical_note="M17.11 27447",
            ),
        )
    finally:
        app.dependency_overrides[get_clock] = lambda: FrozenClock(datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc))
    assert maine.status_code == 422
    assert maine.json()["problems"][0]["code"] == "DOS_IN_PAST"
    assert "America/New_York" in maine.json()["problems"][0]["message"]
    assert hawaii.status_code == 201, hawaii.text


def test_who_sees_what_and_the_decision_names_its_author(client):
    tokens = _tokens()
    created = _submit(client, tokens["office"])
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    assert created.json()["diagnosis_code"] == "M17.11"
    assert "documentation_briefing" not in created.json()
    reviewer_view = client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["reviewer"]))
    assert reviewer_view.json()["documentation_briefing"]["advisory_only"] is True

    queue = client.get("/v1/auth-requests", headers=_auth(tokens["reviewer"]))
    assert queue.status_code == 200
    assert "ZEBRA" not in queue.text

    conflicted = client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["treating"]))
    assert conflicted.status_code == 200
    assert conflicted.json()["treating_conflict"] is True
    assert "ZEBRA" not in conflicted.text
    refused = client.post(f"/v1/auth-requests/{request_id}/pickup", headers=_auth(tokens["treating"]))
    assert refused.status_code == 409
    assert refused.json()["error"] == "treating_conflict"

    casey = _submit(
        client,
        tokens["pacific"],
        _body(
            member_id=str(MEMBER_CASEY),
            procedures=[{"code": "27447", "units": 1}],
            clinical_note="M17.11 planned 27447",
        ),
    )
    assert casey.status_code == 201, casey.text
    allowed = client.post(f"/v1/auth-requests/{casey.json()['id']}/pickup", headers=_auth(tokens["treating"]))
    assert allowed.status_code == 200

    picked = client.post(f"/v1/auth-requests/{request_id}/pickup", headers=_auth(tokens["reviewer"]))
    assert picked.status_code == 200
    assert NOTE in picked.text
    stolen = client.post(f"/v1/auth-requests/{request_id}/pickup", headers=_auth(tokens["reviewer2"]))
    assert stolen.status_code == 404

    blocked = client.post(
        f"/v1/admin/auth-requests/{request_id}/reassign",
        headers=_auth(tokens["admin"]),
        json={"reviewer_id": str(USER_TREATING)},
    )
    assert blocked.status_code == 409
    moved = client.post(
        f"/v1/admin/auth-requests/{request_id}/reassign",
        headers=_auth(tokens["admin"]),
        json={"reviewer_id": str(USER_REVIEWER_2)},
    )
    assert moved.status_code == 200
    late = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer"]),
        json={"lines": _decisions(created.json(), deny_second=True)},
    )
    # The case now belongs to someone else, so it is not visible to the first reviewer.
    assert late.status_code == 404

    incomplete = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer2"]),
        json={"lines": _decisions(created.json(), deny_second=True)[:1]},
    )
    assert incomplete.status_code == 422
    assert client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["admin"])).json()["status"] == "in_review"

    decided = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer2"]),
        json={"lines": _decisions(created.json(), deny_second=True)},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "partial"
    again = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer2"]),
        json={"lines": _decisions(created.json(), deny_second=True)},
    )
    assert again.status_code == 409
    assert again.json()["error"] == "decision_final"

    member = client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["alex"]))
    office = client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["office"]))
    admin = client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["admin"]))
    support = client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["support"]))
    assert member.status_code == 200
    assert "ZEBRA" not in member.text
    assert {line["decision"] for line in member.json()["lines"]} == {"approved", "denied"}
    assert "denial_reason_text" not in member.json()["lines"][0]
    assert REASON in office.text
    assert INTERNAL not in office.text
    assert NOTE in office.text
    assert INTERNAL in admin.text
    assert "ZEBRA" not in support.text

    other = client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["pacific"]))
    assert other.status_code == 404
    history = client.get(f"/v1/auth-requests/{request_id}/history", headers=_auth(tokens["admin"]))
    assert history.status_code == 200
    assert "ZEBRA" not in history.text
    actions = [event["action"] for event in history.json()["events"]]
    assert "decision_recorded" in actions
    decision = next(event for event in history.json()["events"] if event["action"] == "decision_recorded")
    assert decision["actor_id"] == str(USER_REVIEWER_2)

    notices = client.get("/v1/admin/notifications", headers=_auth(tokens["admin"]))
    notice = next(item for item in notices.json()["notifications"] if item["request_id"] == request_id)
    assert REASON in str(notice["payload"])
    assert "ZEBRA-CLINICAL-NOTE" not in str(notice["payload"])
    assert INTERNAL not in str(notice["payload"])

    load = client.get("/v1/admin/reviewer-load", headers=_auth(tokens["admin"]))
    counts = {row["reviewer_id"]: row["open_requests"] for row in load.json()["reviewers"]}
    assert counts[str(USER_TREATING)] == 1
    assert counts[str(USER_REVIEWER)] == 0
    assert counts[str(USER_REVIEWER_2)] == 0
    assert len(counts) == 3


def test_a_model_that_approves_is_not_what_the_reviewer_reads(client):
    tokens = _tokens()

    class DecidingModel:
        name = "scripted"
        model = "bad"

        def brief(self, _packet):
            return Briefing((), (), "APPROVE this procedure")

    app.dependency_overrides[get_briefing_provider] = lambda: DecidingModel()
    try:
        created = _submit(client, tokens["office"])
    finally:
        app.dependency_overrides.pop(get_briefing_provider, None)
    assert created.status_code == 201, created.text
    seen = client.get(f"/v1/auth-requests/{created.json()['id']}", headers=_auth(tokens["admin"]))
    briefing = seen.json()["documentation_briefing"]
    assert briefing["output_rejected"] is True
    assert briefing["advisory_only"] is True
    assert "APPROVE" not in briefing["narrative"].upper()
    assert "approv" not in briefing["narrative"].lower()


def test_approved_without_an_actor_cannot_be_stored(client):
    tokens = _tokens()
    created = _submit(client, tokens["office"], _body(procedures=[{"code": "27447", "units": 1}], clinical_note="M17.11 27447"))
    request_id = created.json()["id"]
    with SessionLocal() as db:
        with pytest_raises_integrity():
            db.execute(
                text(
                    "UPDATE auth_requests SET status = 'approved', decided_by = NULL, decided_at = NULL WHERE id = :id"
                ),
                {"id": request_id},
            )
            db.commit()
        db.rollback()
        with pytest_raises_integrity():
            db.execute(
                text(
                    "UPDATE auth_request_lines SET decision = 'denied', denial_reason_code = NULL, "
                    "denial_reason_text = NULL WHERE request_id = :id"
                ),
                {"id": request_id},
            )
            db.commit()
        db.rollback()
        row = db.get(AuthRequest, uuid.UUID(request_id))
        assert row is not None
        assert row.status == "submitted"
        assert row.decided_by is None
        line = db.scalar(select(AuthRequestLine).where(AuthRequestLine.request_id == row.id))
        assert line is not None
        assert line.decision == "pending"


def test_member_cannot_submit_and_unknown_fields_are_rejected(client):
    tokens = _tokens()
    submitted = _submit(client, tokens["alex"])
    assert submitted.status_code == 403
    extra = _submit(client, tokens["office"], _body(expert=True))
    assert extra.status_code == 422
    assert extra.json()["error"] == "invalid_request"


class pytest_raises_integrity:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        if exc_type is None:
            raise AssertionError("expected IntegrityError")
        return issubclass(exc_type, IntegrityError)


def _decisions(created: dict, *, deny_second: bool) -> list[dict]:
    lines = []
    for index, line in enumerate(created["lines"]):
        if deny_second and index == 1:
            lines.append(
                {
                    "line_id": line["id"],
                    "decision": "denied",
                    "denial_reason_code": "insufficient_documentation",
                    "denial_reason_text": REASON,
                    "internal_comment": INTERNAL,
                }
            )
        else:
            lines.append({"line_id": line["id"], "decision": "approved", "internal_comment": INTERNAL})
    return lines
