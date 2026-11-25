# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Tables. This file is the schema.

Alembic applies Base.metadata.create_all so the database cannot drift from these
classes. The check named ck_decided_request_has_actor is the fix for the audit
that found a request marked approved with nobody recorded as the approver.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Practice(Base):
    __tablename__ = "practices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    npi: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)


class Member(Base):
    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    member_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    given_name: Mapped[str] = mapped_column(String(100), nullable=False)
    family_name: Mapped[str] = mapped_column(String(100), nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('member', 'provider', 'reviewer', 'admin', 'support')",
            name="ck_user_role",
        ),
        CheckConstraint(
            "(role <> 'provider') OR (practice_id IS NOT NULL)",
            name="ck_provider_has_practice",
        ),
        CheckConstraint(
            "(role <> 'member') OR (member_id IS NOT NULL)",
            name="ck_member_user_has_member",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    practice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("practices.id"), nullable=True)
    member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("members.id"), nullable=True)


class Coverage(Base):
    """Terminated_on is inclusive: the member is covered on that calendar date."""

    __tablename__ = "coverage"
    __table_args__ = (
        CheckConstraint(
            "terminated_on IS NULL OR terminated_on >= effective_on",
            name="ck_coverage_dates",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("members.id"), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(200), nullable=False)
    effective_on: Mapped[date] = mapped_column(Date, nullable=False)
    terminated_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class MemberPractice(Base):
    """Unlinked_on is exclusive: the link is already over on that calendar date."""

    __tablename__ = "member_practices"
    __table_args__ = (
        CheckConstraint(
            "unlinked_on IS NULL OR unlinked_on > linked_on",
            name="ck_link_dates",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("members.id"), nullable=False)
    practice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practices.id"), nullable=False)
    linked_on: Mapped[date] = mapped_column(Date, nullable=False)
    unlinked_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class TreatingRelationship(Base):
    """A reviewer who treats this member cannot decide their prior auth.

    ended_on null means the relationship is current. A closed relationship
    does not block review. See README for why this is present-tense only.
    """

    __tablename__ = "treating_relationships"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("members.id"), nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class AuthRequest(Base):
    __tablename__ = "auth_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted', 'in_review', 'approved', 'denied', 'partial')",
            name="ck_request_status",
        ),
        # The audit finding: approved/denied/partial with no human on the row.
        CheckConstraint(
            "status NOT IN ('approved', 'denied', 'partial') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_decided_request_has_actor",
        ),
        Index("ix_auth_requests_queue", "status", "reviewer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    practice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practices.id"), nullable=False)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("members.id"), nullable=False)
    submitted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    diagnosis_code: Mapped[str] = mapped_column(String(8), nullable=False)
    date_of_service: Mapped[date] = mapped_column(Date, nullable=False)
    clinical_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted")
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    lines: Mapped[list["AuthRequestLine"]] = relationship(
        back_populates="request",
        order_by="AuthRequestLine.position",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    briefings: Mapped[list["AiBriefing"]] = relationship(
        back_populates="request",
        order_by="AiBriefing.created_at",
        lazy="selectin",
    )


class AuthRequestLine(Base):
    __tablename__ = "auth_request_lines"
    __table_args__ = (
        UniqueConstraint("request_id", "position", name="uq_line_position"),
        CheckConstraint("units >= 1 AND units <= 200", name="ck_line_units"),
        CheckConstraint("decision IN ('pending', 'approved', 'denied')", name="ck_line_decision"),
        CheckConstraint(
            "decision <> 'denied' OR (denial_reason_code IS NOT NULL AND denial_reason_text IS NOT NULL AND btrim(denial_reason_text) <> '')",
            name="ck_denial_has_reason",
        ),
        CheckConstraint(
            "decision <> 'approved' OR denial_reason_text IS NULL",
            name="ck_approval_has_no_denial_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth_requests.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(5), nullable=False)
    code_system: Mapped[str] = mapped_column(String(8), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    units_confirmed: Mapped[bool] = mapped_column(nullable=False, default=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    denial_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    denial_reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    request: Mapped[AuthRequest] = relationship(back_populates="lines")


class SubmissionAttempt(Base):
    """Every submit, including the ones we reject.

    claimed_member_id is not a foreign key. Support has to be able to look up
    a member id that was typed wrong and therefore is not in `members`.
    """

    __tablename__ = "submission_attempts"
    __table_args__ = (
        UniqueConstraint("practice_id", "idempotency_key", name="uq_attempt_idempotency"),
        CheckConstraint("outcome IN ('accepted', 'rejected')", name="ck_attempt_outcome"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    practice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practices.id"), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    claimed_member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    problems: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("auth_requests.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("auth_requests.id"), nullable=True)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("submission_attempts.id"), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class AiBriefing(Base):
    """Advisory documentation check. Never a coverage decision."""

    __tablename__ = "ai_briefings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth_requests.id"), nullable=False)
    attempted_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    missing_information: Mapped[list] = mapped_column(JSONB, nullable=False)
    extracted_facts: Mapped[list] = mapped_column(JSONB, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    output_rejected: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    request: Mapped[AuthRequest] = relationship(back_populates="briefings")


class NotificationOutbox(Base):
    """Decision notices waiting for a BAA-covered sender. This process does not send them."""

    __tablename__ = "notification_outbox"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth_requests.id"), nullable=False)
    practice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practices.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
