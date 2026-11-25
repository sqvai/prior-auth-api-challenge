# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import service
from app.api import get_clock
from app.auth import AuthError, _b64, current_user, decode_token, issue_token
from app.clock import Clock, FrozenClock
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import (
    AiBriefing,
    AuthRequest,
    AuthRequestLine,
    Coverage,
    MemberPractice,
    Practice,
    SubmissionAttempt,
    TreatingRelationship,
    User,
)
from app.policy import can_see_request, list_clause
from app.seed import (
    MEMBER_ALEX,
    MEMBER_BLAIR,
    MEMBER_CASEY,
    PRACTICE_ME,
    USER_ALEX,
    USER_OFFICE_ME,
    USER_REVIEWER,
    USER_TREATING,
    main,
    seed,
)
from tests.test_api import INTERNAL, NOTE, REASON, _auth, _body, _submit, _tokens


def test_clock_practice_zones_and_naive_freeze_is_refused():
    now = get_clock().now("Pacific/Honolulu")
    assert now.tzinfo is not None
    assert Clock().now("UTC").tzinfo is not None
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 11, 24))


def test_blank_settings_are_unset_and_non_strings_pass_through():
    blank = Settings(
        database_url="postgresql+psycopg://priorauth:priorauth@127.0.0.1:54329/priorauth_test",
        token_secret="test-secret",
        ai_base_url="",
        ai_api_key="   ",
    )
    assert blank.ai_base_url is None
    assert blank.ai_api_key is None
    assert Settings.blank_is_unset("http://llm.example") == "http://llm.example"
    assert Settings.blank_is_unset(None) is None


def test_tokens_that_are_stale_forged_or_mismatched_are_rejected(client):
    tokens = _tokens()
    me = client.get("/v1/me", headers=_auth(tokens["office"]))
    assert me.status_code == 200
    assert me.json()["practice_id"] == str(PRACTICE_ME)
    assert client.get("/v1/me", headers={"Authorization": "Basic abc"}).status_code == 401
    expired = issue_token(USER_OFFICE_ME, "provider", "test-secret", -30)
    assert client.get("/v1/me", headers=_auth(expired)).status_code == 401
    wrong_role = issue_token(USER_OFFICE_ME, "admin", "test-secret", 3600)
    assert client.get("/v1/me", headers=_auth(wrong_role)).status_code == 401
    missing_user = issue_token(uuid.uuid4(), "provider", "test-secret", 3600)
    assert client.get("/v1/me", headers=_auth(missing_user)).status_code == 401
    with pytest.raises(AuthError):
        decode_token("x" * 5000, "test-secret")
    with pytest.raises(AuthError):
        decode_token("not-a-token", "test-secret")
    with pytest.raises(AuthError):
        decode_token("v2.a.b", "test-secret")
    flipped = tokens["office"][:-1] + ("A" if tokens["office"][-1] != "A" else "B")
    with pytest.raises(AuthError):
        decode_token(flipped, "test-secret")
    for payload in (
        {"role": "provider", "exp": 9_999_999_999},
        {"sub": "not-a-uuid", "role": "provider", "exp": 9_999_999_999},
    ):
        body = _b64(json.dumps(payload).encode())
        signature = _b64(hmac.new(b"test-secret", body.encode(), hashlib.sha256).digest())
        with pytest.raises(AuthError):
            decode_token(f"v1.{body}.{signature}", "test-secret")
    garbage = _b64(b"not-json")
    signature = _b64(hmac.new(b"test-secret", garbage.encode(), hashlib.sha256).digest())
    with pytest.raises(AuthError):
        decode_token(f"v1.{garbage}.{signature}", "test-secret")
    with SessionLocal() as db:
        with pytest.raises(Exception) as basic:
            current_user(SimpleNamespace(scheme="Basic", credentials="abc"), db, get_settings())
        assert basic.value.status_code == 401


def test_request_id_is_echoed_and_health_hides_a_down_database(client, monkeypatch):
    specified = client.get("/health", headers={"X-Request-Id": "trace-1"})
    assert specified.headers["x-request-id"] == "trace-1"
    generated = client.get("/health")
    assert generated.headers["x-request-id"]

    class Down:
        def __call__(self):
            return self

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("password=super-secret")

        def close(self):
            return None

    monkeypatch.setattr("app.main.SessionLocal", Down())
    failed = client.get("/health")
    assert failed.status_code == 503
    assert "super-secret" not in failed.text


def test_unexpected_errors_do_not_leak_and_bad_bodies_name_the_field(client, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    tokens = _tokens()

    def boom(*_args, **_kwargs):
        raise RuntimeError("postgres password=super-secret")

    monkeypatch.setattr("app.api.list_requests", boom)
    with TestClient(app, raise_server_exceptions=False) as quiet:
        failed = quiet.get("/v1/auth-requests", headers=_auth(tokens["office"]))
    assert failed.status_code == 500
    assert failed.json()["error"] == "internal_error"
    assert "super-secret" not in failed.text

    empty = _submit(client, tokens["office"], _body(procedures=[]))
    assert empty.status_code == 422
    assert empty.json()["problems"][0]["code"] == "INVALID_FIELD"
    too_long = _submit(client, tokens["office"], _body(clinical_note="n" * 20_001))
    assert too_long.status_code == 422
    too_many = _submit(
        client,
        tokens["office"],
        _body(procedures=[{"code": f"{10000 + index}"[-5:], "units": 1} for index in range(21)]),
    )
    assert too_many.status_code == 422


def test_idempotency_edges(client):
    tokens = _tokens()
    blank = _submit(client, tokens["office"], key="   ")
    assert blank.status_code == 400
    huge = _submit(client, tokens["office"], key="k" * 201)
    assert huge.status_code == 400
    spaced = "  office-key  "
    first = _submit(client, tokens["office"], key=spaced)
    assert first.status_code == 201
    replay = _submit(client, tokens["office"], key="office-key")
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    bad_key = "bad-once"
    bad_body = _body(member_id=str(uuid.uuid4()))
    rejected = _submit(client, tokens["office"], bad_body, key=bad_key)
    assert rejected.status_code == 422
    again = _submit(client, tokens["office"], bad_body, key=bad_key)
    assert again.status_code == 422
    assert again.json()["idempotent_replay"] is True
    assert again.json()["attempt_id"] == rejected.json()["attempt_id"]
    with SessionLocal() as db:
        count = db.scalar(
            select(SubmissionAttempt).where(SubmissionAttempt.idempotency_key == "bad-once")
        )
        assert count is not None


def test_date_unit_and_code_boundaries(client):
    tokens = _tokens()
    same_day = _submit(
        client,
        tokens["office"],
        _body(date_of_service="2026-11-24", procedures=[{"code": "27447", "units": 1}], clinical_note="M17.11 27447"),
    )
    assert same_day.status_code == 201, same_day.text
    anniversary = _submit(
        client,
        tokens["office"],
        _body(date_of_service="2027-11-24", procedures=[{"code": "27447", "units": 1}], clinical_note="M17.11 27447"),
    )
    assert anniversary.status_code == 201, anniversary.text
    too_far = _submit(
        client,
        tokens["office"],
        _body(date_of_service="2027-11-25", procedures=[{"code": "27447", "units": 1}], clinical_note="M17.11 27447"),
    )
    assert too_far.json()["problems"][0]["code"] == "DOS_TOO_FAR"

    def units(count, confirm, code="27447"):
        return _submit(
            client,
            tokens["office"],
            _body(
                procedures=[{"code": code, "units": count}],
                confirm_high_units=confirm,
                clinical_note=f"M17.11 {code}",
            ),
        )

    assert units(0, False).json()["problems"][0]["code"] == "UNITS_OUT_OF_RANGE"
    thirty = units(30, False)
    assert thirty.status_code == 201
    assert thirty.json()["lines"][0]["units_confirmed"] is False
    assert units(31, False).json()["problems"][0]["code"] == "UNITS_LIKELY_TYPO"
    confirmed = units(200, True)
    assert confirmed.status_code == 201
    assert confirmed.json()["lines"][0]["units_confirmed"] is True
    assert units(201, True).json()["problems"][0]["code"] == "UNITS_OUT_OF_RANGE"
    hcpcs = units(1, False, code="A0428")
    assert hcpcs.status_code == 201
    assert hcpcs.json()["lines"][0]["code_system"] == "HCPCS"
    modifier = _submit(
        client,
        tokens["office"],
        _body(procedures=[{"code": "27447-RT", "units": 1}], clinical_note="M17.11"),
    )
    assert modifier.json()["problems"][0]["code"] == "PROCEDURE_CODE_INVALID"

    blank_note = _submit(client, tokens["office"], _body(clinical_note="  \n  "))
    assert blank_note.status_code == 201
    detail = client.get(f"/v1/auth-requests/{blank_note.json()['id']}", headers=_auth(tokens["reviewer"]))
    assert detail.json()["clinical_note"] is None
    assert "not attached" in " ".join(detail.json()["documentation_briefing"]["missing_information"])


def test_coverage_and_link_boundaries_follow_the_calendar_day(client):
    tokens = _tokens()
    with SessionLocal() as db:
        blair = db.scalar(select(Coverage).where(Coverage.member_id == MEMBER_BLAIR))
        blair.terminated_on = date(2026, 12, 24)
        alex_link = db.scalar(select(MemberPractice).where(MemberPractice.member_id == MEMBER_ALEX))
        alex_link.unlinked_on = date(2026, 12, 24)
        db.commit()
    last_covered = _submit(client, tokens["office"], _body(member_id=str(MEMBER_BLAIR), date_of_service="2026-12-24"))
    assert last_covered.status_code == 201, last_covered.text
    day_after = _submit(client, tokens["office"], _body(member_id=str(MEMBER_BLAIR), date_of_service="2026-12-25"))
    assert any(item["code"] == "COVERAGE_INACTIVE" for item in day_after.json()["problems"])
    still_linked = _submit(client, tokens["office"], _body(date_of_service="2026-12-23"))
    assert still_linked.status_code == 201, still_linked.text
    unlinked = _submit(client, tokens["office"], _body(date_of_service="2026-12-24"))
    assert any(item["code"] == "MEMBER_NOT_LINKED" for item in unlinked.json()["problems"])

    with SessionLocal() as db:
        alex = db.scalar(select(Coverage).where(Coverage.member_id == MEMBER_ALEX))
        alex.effective_on = date(2026, 12, 25)
        db.execute(delete(Coverage).where(Coverage.member_id == MEMBER_CASEY))
        db.commit()
    future = _submit(client, tokens["office"], _body(date_of_service="2026-12-23"))
    assert "does not start until 2026-12-25" in future.text
    missing = _submit(client, tokens["pacific"], _body(member_id=str(MEMBER_CASEY)))
    assert "no coverage on file" in missing.text


def test_a_closed_treating_relationship_does_not_block_review(client):
    tokens = _tokens()
    with SessionLocal() as db:
        row = db.scalar(select(TreatingRelationship).where(TreatingRelationship.reviewer_user_id == USER_TREATING))
        row.ended_on = date(2020, 6, 1)
        db.commit()
    created = _submit(client, tokens["office"])
    picked = client.post(f"/v1/auth-requests/{created.json()['id']}/pickup", headers=_auth(tokens["treating"]))
    assert picked.status_code == 200, picked.text


def test_lists_filters_support_search_and_an_unknown_role_sees_nothing(client):
    tokens = _tokens()
    mine = _submit(client, tokens["office"])
    theirs = _submit(
        client,
        tokens["pacific"],
        _body(member_id=str(MEMBER_CASEY), procedures=[{"code": "27447", "units": 1}], clinical_note="M17.11 27447"),
    )
    assert theirs.status_code == 201
    office = client.get("/v1/auth-requests", headers=_auth(tokens["office"]))
    assert {item["id"] for item in office.json()["requests"]} == {mine.json()["id"]}
    assert client.get(f"/v1/auth-requests/{mine.json()['id']}", headers=_auth(tokens["office"])).status_code == 200
    assert client.get("/v1/auth-requests", headers=_auth(tokens["alex"])).json()["requests"]
    assert "clinical_note" not in office.json()["requests"][0]
    admin = client.get("/v1/auth-requests", headers=_auth(tokens["admin"]), params={"status": "submitted", "limit": 1})
    assert len(admin.json()["requests"]) == 1
    assert admin.json()["limit"] == 1
    empty = client.get("/v1/auth-requests", headers=_auth(tokens["admin"]), params={"offset": 50})
    assert empty.json()["requests"] == []
    bad_status = client.get("/v1/auth-requests", headers=_auth(tokens["office"]), params={"status": "nope"})
    assert bad_status.status_code == 422
    missing = client.get(f"/v1/auth-requests/{uuid.uuid4()}", headers=_auth(tokens["admin"]))
    assert missing.status_code == 404
    hidden = client.get(f"/v1/auth-requests/{mine.json()['id']}", headers=_auth(tokens["alex"]))
    # Alex's own request is visible. Casey's is not.
    assert hidden.status_code == 200
    assert client.get(f"/v1/auth-requests/{theirs.json()['id']}", headers=_auth(tokens["alex"])).status_code == 404

    rejected = _submit(client, tokens["office"], _body(diagnosis_code="nope"))
    by_member = client.get(
        "/v1/support/attempts",
        headers=_auth(tokens["support"]),
        params={"member_id": str(MEMBER_ALEX), "practice_id": str(PRACTICE_ME)},
    )
    assert rejected.json()["attempt_id"] in {item["id"] for item in by_member.json()["attempts"]}
    one = client.get(
        "/v1/support/attempts",
        headers=_auth(tokens["support"]),
        params={"attempt_id": mine.json()["attempt_id"]},
    )
    assert one.json()["attempts"][0]["request_id"] == mine.json()["id"]
    assert client.get("/v1/support/attempts", headers=_auth(tokens["office"])).status_code == 403
    assert client.get("/v1/admin/reviewer-load", headers=_auth(tokens["reviewer"])).status_code == 403
    assert client.get("/v1/admin/notifications", headers=_auth(tokens["support"])).status_code == 403

    with SessionLocal() as db:
        auditor = SimpleNamespace(role="auditor", id=uuid.uuid4(), member_id=None, practice_id=None)
        request = db.get(AuthRequest, uuid.UUID(mine.json()["id"]))
        assert can_see_request(auditor, request) is False
        assert list_clause(auditor) is not None
        listed = service.list_requests(db, auditor, None, 10, 0)
        assert listed.body["requests"] == []


def test_review_edges_and_both_unanimous_outcomes(client):
    tokens = _tokens()
    assert client.post(f"/v1/auth-requests/{uuid.uuid4()}/pickup", headers=_auth(tokens["alex"])).status_code == 403
    created = _submit(client, tokens["office"])
    request_id = created.json()["id"]
    early = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer"]),
        json={"lines": [{"line_id": created.json()["lines"][0]["id"], "decision": "approved"}]},
    )
    assert early.status_code == 409
    assert early.json()["error"] == "not_your_request"
    assert client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["office"]),
        json={"lines": [{"line_id": created.json()["lines"][0]["id"], "decision": "approved"}]},
    ).status_code == 403
    assert client.get(f"/v1/auth-requests/{request_id}/history", headers=_auth(tokens["alex"])).status_code == 403
    assert client.get(f"/v1/auth-requests/{request_id}/history", headers=_auth(tokens["support"])).status_code == 403
    assert client.get(f"/v1/auth-requests/{request_id}/history", headers=_auth(tokens["treating"])).status_code == 403
    assert client.get(f"/v1/auth-requests/{uuid.uuid4()}/history", headers=_auth(tokens["admin"])).status_code == 404
    queue_history = client.get(f"/v1/auth-requests/{request_id}/history", headers=_auth(tokens["reviewer"]))
    assert "submission_accepted" in {event["action"] for event in queue_history.json()["events"]}

    picked = client.post(f"/v1/auth-requests/{request_id}/pickup", headers=_auth(tokens["reviewer"]))
    assert picked.status_code == 200
    load = client.get("/v1/admin/reviewer-load", headers=_auth(tokens["admin"]))
    open_counts = [row["open_requests"] for row in load.json()["reviewers"]]
    assert 1 in open_counts
    assert 0 in open_counts
    assert client.post(
        f"/v1/admin/auth-requests/{uuid.uuid4()}/reassign",
        headers=_auth(tokens["admin"]),
        json={"reviewer_id": str(USER_REVIEWER)},
    ).status_code == 404
    again = client.post(f"/v1/auth-requests/{request_id}/pickup", headers=_auth(tokens["reviewer"]))
    assert again.status_code == 200

    line_ids = [line["id"] for line in created.json()["lines"]]
    duplicate = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer"]),
        json={"lines": [{"line_id": line_ids[0], "decision": "approved"}, {"line_id": line_ids[0], "decision": "approved"}]},
    )
    assert {item["code"] for item in duplicate.json()["problems"]} >= {"DUPLICATE_LINE", "LINES_MUST_MATCH"}
    unknown = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer"]),
        json={
            "lines": [
                {"line_id": line_ids[0], "decision": "approved"},
                {"line_id": str(uuid.uuid4()), "decision": "denied", "denial_reason_code": "other", "denial_reason_text": "x"},
            ]
        },
    )
    assert any(item["code"] == "LINES_MUST_MATCH" for item in unknown.json()["problems"])
    maybe = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer"]),
        json={"lines": [{"line_id": line_ids[0], "decision": "maybe"}, {"line_id": line_ids[1], "decision": "approved"}]},
    )
    assert any(item["code"] == "DECISION_INVALID" for item in maybe.json()["problems"])
    with_reason = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer"]),
        json={
            "lines": [
                {"line_id": line_ids[0], "decision": "approved", "denial_reason_text": "nope"},
                {"line_id": line_ids[1], "decision": "denied", "denial_reason_code": "nope", "denial_reason_text": "   "},
            ]
        },
    )
    codes = {item["code"] for item in with_reason.json()["problems"]}
    assert "APPROVAL_HAS_DENIAL_REASON" in codes
    assert "DENIAL_REASON_REQUIRED" in codes
    assert client.get(f"/v1/auth-requests/{request_id}", headers=_auth(tokens["admin"])).json()["status"] == "in_review"

    handed = client.post(
        f"/v1/admin/auth-requests/{request_id}/reassign",
        headers=_auth(tokens["office"]),
        json={"reviewer_id": str(USER_REVIEWER)},
    )
    assert handed.status_code == 403
    missing_reviewer = client.post(
        f"/v1/admin/auth-requests/{request_id}/reassign",
        headers=_auth(tokens["admin"]),
        json={"reviewer_id": str(USER_ALEX)},
    )
    assert missing_reviewer.status_code == 422

    fresh = _submit(
        client, tokens["office"], _body(procedures=[{"code": "99213", "units": 1}], clinical_note="M17.11 99213")
    )
    client.post(
        f"/v1/admin/auth-requests/{fresh.json()['id']}/reassign",
        headers=_auth(tokens["admin"]),
        json={"reviewer_id": str(USER_REVIEWER)},
    )
    history = client.get(f"/v1/auth-requests/{fresh.json()['id']}/history", headers=_auth(tokens["admin"]))
    reassigned = next(event for event in history.json()["events"] if event["action"] == "reassigned")
    assert reassigned["detail"]["from_reviewer_id"] is None

    with SessionLocal() as db:
        db.add(TreatingRelationship(reviewer_user_id=USER_REVIEWER, member_id=MEMBER_ALEX, ended_on=None))
        db.commit()
    blocked = client.post(
        f"/v1/auth-requests/{request_id}/decision",
        headers=_auth(tokens["reviewer"]),
        json={
            "lines": [
                {"line_id": line_ids[0], "decision": "approved"},
                {"line_id": line_ids[1], "decision": "approved"},
            ]
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "treating_conflict"
    still = client.get(f"/v1/auth-requests/{request_id}/history", headers=_auth(tokens["reviewer"]))
    assert still.status_code == 200

    approved = _submit(
        client,
        tokens["pacific"],
        _body(member_id=str(MEMBER_CASEY), procedures=[{"code": "27447", "units": 1}], clinical_note="M17.11 27447"),
    )
    client.post(f"/v1/auth-requests/{approved.json()['id']}/pickup", headers=_auth(tokens["reviewer"]))
    yes = client.post(
        f"/v1/auth-requests/{approved.json()['id']}/decision",
        headers=_auth(tokens["reviewer"]),
        json={"lines": [{"line_id": approved.json()["lines"][0]["id"], "decision": "approved", "internal_comment": "   "}]},
    )
    assert yes.status_code == 200, yes.text
    assert yes.json()["status"] == "approved"
    notices = client.get("/v1/admin/notifications", headers=_auth(tokens["admin"]))
    assert any(item["request_id"] == approved.json()["id"] for item in notices.json()["notifications"])
    second = client.post(
        f"/v1/auth-requests/{approved.json()['id']}/decision",
        headers=_auth(tokens["reviewer"]),
        json={"lines": [{"line_id": approved.json()["lines"][0]["id"], "decision": "approved"}]},
    )
    assert second.status_code == 409
    assert second.json()["error"] == "decision_final"
    assert yes.json()["lines"][0]["internal_comment"] is None
    assert yes.json()["decided_at"]
    done = client.post(f"/v1/auth-requests/{approved.json()['id']}/pickup", headers=_auth(tokens["reviewer"]))
    assert done.status_code == 409
    assert done.json()["error"] == "decision_final"
    assert client.post(
        f"/v1/admin/auth-requests/{approved.json()['id']}/reassign",
        headers=_auth(tokens["admin"]),
        json={"reviewer_id": str(USER_REVIEWER)},
    ).status_code == 409

    denied = _submit(
        client,
        tokens["pacific"],
        _body(member_id=str(MEMBER_CASEY), procedures=[{"code": "99214", "units": 1}], clinical_note="M17.11 99214"),
    )
    client.post(f"/v1/auth-requests/{denied.json()['id']}/pickup", headers=_auth(tokens["reviewer"]))
    no = client.post(
        f"/v1/auth-requests/{denied.json()['id']}/decision",
        headers=_auth(tokens["reviewer"]),
        json={
            "lines": [
                {
                    "line_id": denied.json()["lines"][0]["id"],
                    "decision": "denied",
                    "denial_reason_code": "other",
                    "denial_reason_text": "  not a covered benefit on this plan  ",
                }
            ]
        },
    )
    assert no.json()["status"] == "denied"
    assert no.json()["lines"][0]["denial_reason_text"] == "not a covered benefit on this plan"
    with SessionLocal() as db:
        row = db.scalar(select(AuthRequestLine).where(AuthRequestLine.id == uuid.UUID(denied.json()["lines"][0]["id"])))
        assert row.decided_by == USER_REVIEWER
        db.execute(delete(AiBriefing).where(AiBriefing.request_id == uuid.UUID(denied.json()["id"])))
        db.commit()
    bare = client.get(f"/v1/auth-requests/{denied.json()['id']}", headers=_auth(tokens["admin"]))
    assert "documentation_briefing" not in bare.json()


def test_model_outage_falls_back_and_a_double_failure_stays_advisory(client, monkeypatch):
    tokens = _tokens()

    class Down:
        name = "remote"
        model = "remote"

        def brief(self, _packet):
            raise ConnectionError("connection reset")

    from app.api import get_briefing_provider

    client.app.dependency_overrides[get_briefing_provider] = lambda: Down()
    try:
        created = _submit(client, tokens["office"])
    finally:
        client.app.dependency_overrides.pop(get_briefing_provider, None)
    assert created.status_code == 201, created.text
    seen = client.get(f"/v1/auth-requests/{created.json()['id']}", headers=_auth(tokens["admin"]))
    briefing = seen.json()["documentation_briefing"]
    assert briefing["output_rejected"] is True
    assert briefing["provider"] == "documentation-checker"
    history = client.get(f"/v1/auth-requests/{created.json()['id']}/history", headers=_auth(tokens["admin"]))
    assert "ai_briefing_failed" in {event["action"] for event in history.json()["events"]}

    class Refuse:
        name = "remote"
        model = "remote"

        def brief(self, _packet):
            from app.ai import AiOutputRejected

            raise AiOutputRejected()

    class AlsoDown:
        name = "documentation-checker"
        model = "rules-v1"

        def brief(self, _packet):
            raise RuntimeError("checker down")

    monkeypatch.setattr(service, "DocumentationChecker", AlsoDown)
    client.app.dependency_overrides[get_briefing_provider] = lambda: Refuse()
    try:
        second = _submit(
            client,
            tokens["office"],
            _body(procedures=[{"code": "99215", "units": 1}], clinical_note="M17.11 99215"),
        )
    finally:
        client.app.dependency_overrides.pop(get_briefing_provider, None)
    assert second.status_code == 201, second.text
    admin = client.get(f"/v1/auth-requests/{second.json()['id']}", headers=_auth(tokens["admin"]))
    fallback = admin.json()["documentation_briefing"]
    assert fallback["provider"] == "static-fallback"
    assert "determination" in fallback["narrative"]
    assert "approv" not in fallback["narrative"].lower()


def test_lost_idempotency_race_replays_the_winner(client, monkeypatch):
    tokens = _tokens()
    key = "race-key"
    first = _submit(client, tokens["office"], key=key)
    assert first.status_code == 201
    real = service._attempt_for
    state = {"skip": True}

    def skip_once(*args, **kwargs):
        if state["skip"]:
            state["skip"] = False
            return None
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "_attempt_for", skip_once)
    second = _submit(client, tokens["office"], key=key)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["idempotent_replay"] is True


def test_a_constraint_failure_without_a_prior_attempt_stays_internal(client, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    tokens = _tokens()

    def explode(_self):
        raise IntegrityError("INSERT", {}, Exception("forced-duplicate"))

    monkeypatch.setattr(Session, "begin_nested", explode)
    with TestClient(app, raise_server_exceptions=False) as quiet:
        failed = quiet.post(
            "/v1/auth-requests",
            headers=_auth(tokens["office"], "forced-integrity"),
            json=_body(),
        )
    assert failed.status_code == 500
    assert "forced-duplicate" not in failed.text


def test_direct_defensive_branches(client):
    tokens = _tokens()
    _submit(client, tokens["office"])
    with SessionLocal() as db:
        office = db.get(User, USER_OFFICE_ME)
        missing = service.submit_request(
            db,
            User(id=uuid.uuid4(), role="provider", display_name="ghost", practice_id=uuid.uuid4()),
            service.SubmitIn.model_validate(_body()),
            "ghost-key",
            get_clock(),
            service.DocumentationChecker(),
        )
        assert missing.status_code == 500
        assert missing.body["error"] == "practice_missing"

        practice = db.get(Practice, PRACTICE_ME)
        practice.timezone = "Not/AZone"
        db.commit()
        zone = _submit(client, tokens["office"], _body(procedures=[{"code": "11111", "units": 1}], clinical_note="M17.11 11111"))
        assert zone.status_code == 500
        assert zone.json()["error"] == "practice_timezone_invalid"
        practice.timezone = "America/New_York"
        db.commit()

        attempt = SubmissionAttempt(
            practice_id=PRACTICE_ME,
            actor_id=office.id,
            claimed_member_id=MEMBER_ALEX,
            idempotency_key="direct",
            body_hash="abc",
            outcome="accepted",
            problems=[],
            request_id=uuid.uuid4(),
        )
        conflict = service._replay(db, office, attempt, "different")
        assert conflict.status_code == 409
        gone = service._replay(db, office, attempt, "abc")
        assert gone.status_code == 500
        attempt.request_id = None
        empty = service._replay(db, office, attempt, "abc")
        assert empty.status_code == 422
        db.rollback()


def test_seed_can_run_twice_and_prints_dev_tokens(client, capsys):
    with SessionLocal() as db:
        seed(db)
        db.commit()
    main()
    printed = capsys.readouterr().out
    assert "Northshore office" in printed
    assert printed.count("v1.") >= 8


def test_note_constants_stay_out_of_member_payloads(client):
    tokens = _tokens()
    created = _submit(client, tokens["office"])
    assert NOTE.split()[0] in created.text
    assert REASON not in created.text
    assert INTERNAL not in created.text
