# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""What "active on the date of service" means. UM will change this. It lives here."""

import uuid
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Coverage, MemberPractice, TreatingRelationship


def active_link(db: Session, member_id: uuid.UUID, practice_id: uuid.UUID, day: date) -> MemberPractice | None:
    return db.scalar(
        select(MemberPractice).where(
            MemberPractice.member_id == member_id,
            MemberPractice.practice_id == practice_id,
            MemberPractice.linked_on <= day,
            or_(MemberPractice.unlinked_on.is_(None), MemberPractice.unlinked_on > day),
        )
    )


def active_coverage(db: Session, member_id: uuid.UUID, day: date) -> Coverage | None:
    return db.scalar(
        select(Coverage).where(
            Coverage.member_id == member_id,
            Coverage.effective_on <= day,
            or_(Coverage.terminated_on.is_(None), Coverage.terminated_on >= day),
        )
    )


def latest_coverage(db: Session, member_id: uuid.UUID) -> Coverage | None:
    """Used only to explain an inactive-coverage rejection. Not a coverage decision."""
    return db.scalar(
        select(Coverage).where(Coverage.member_id == member_id).order_by(Coverage.effective_on.desc()).limit(1)
    )


def currently_treats(db: Session, reviewer_user_id: uuid.UUID, member_id: uuid.UUID) -> bool:
    row = db.scalar(
        select(TreatingRelationship.id).where(
            TreatingRelationship.reviewer_user_id == reviewer_user_id,
            TreatingRelationship.member_id == member_id,
            TreatingRelationship.ended_on.is_(None),
        )
    )
    return row is not None
