# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Synthetic identities. These people are not real. Do not replace them with PHI."""

import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.auth import issue_token
from app.config import get_settings
from app.models import Coverage, Member, MemberPractice, Practice, TreatingRelationship, User

PRACTICE_ME = uuid.UUID("11111111-1111-4111-8111-111111111111")
PRACTICE_HI = uuid.UUID("22222222-2222-4222-8222-222222222222")
MEMBER_ALEX = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
MEMBER_BLAIR = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2")
MEMBER_CASEY = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3")
USER_OFFICE_ME = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1")
USER_OFFICE_HI = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2")
USER_ALEX = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3")
USER_REVIEWER = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4")
USER_TREATING = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb5")
USER_REVIEWER_2 = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb6")
USER_ADMIN = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb7")
USER_SUPPORT = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb8")

_OPENED = date(2020, 1, 1)


def seed(db: Session) -> None:
    if db.get(Practice, PRACTICE_ME) is not None:
        return
    db.add(Practice(id=PRACTICE_ME, name="Northshore Family Medicine", npi="1999999991", timezone="America/New_York"))
    db.add(Practice(id=PRACTICE_HI, name="Pacific Orthopedics", npi="1999999992", timezone="Pacific/Honolulu"))
    db.add(
        Member(
            id=MEMBER_ALEX,
            member_number="SYN-10001",
            given_name="Alex",
            family_name="Rivera",
            birth_date=date(1984, 4, 12),
        )
    )
    db.add(
        Member(
            id=MEMBER_BLAIR,
            member_number="SYN-10002",
            given_name="Blair",
            family_name="Nguyen",
            birth_date=date(1972, 9, 3),
        )
    )
    db.add(
        Member(
            id=MEMBER_CASEY,
            member_number="SYN-10003",
            given_name="Casey",
            family_name="Ito",
            birth_date=date(1991, 1, 28),
        )
    )
    db.flush()
    db.add(Coverage(member_id=MEMBER_ALEX, plan_name="Synthetic PPO", effective_on=_OPENED, terminated_on=None))
    db.add(
        Coverage(member_id=MEMBER_BLAIR, plan_name="Synthetic PPO", effective_on=_OPENED, terminated_on=date(2026, 1, 15))
    )
    db.add(Coverage(member_id=MEMBER_CASEY, plan_name="Synthetic PPO", effective_on=_OPENED, terminated_on=None))
    db.add(MemberPractice(member_id=MEMBER_ALEX, practice_id=PRACTICE_ME, linked_on=_OPENED, unlinked_on=None))
    db.add(MemberPractice(member_id=MEMBER_BLAIR, practice_id=PRACTICE_ME, linked_on=_OPENED, unlinked_on=None))
    db.add(MemberPractice(member_id=MEMBER_CASEY, practice_id=PRACTICE_HI, linked_on=_OPENED, unlinked_on=None))
    users = [
        User(id=USER_OFFICE_ME, role="provider", display_name="Northshore front desk", practice_id=PRACTICE_ME),
        User(id=USER_OFFICE_HI, role="provider", display_name="Pacific front desk", practice_id=PRACTICE_HI),
        User(id=USER_ALEX, role="member", display_name="Alex Rivera", member_id=MEMBER_ALEX),
        User(id=USER_REVIEWER, role="reviewer", display_name="Riley Okonkwo, RN"),
        User(id=USER_TREATING, role="reviewer", display_name="Jordan Blake, MD"),
        User(id=USER_REVIEWER_2, role="reviewer", display_name="Sam Patel, RN"),
        User(id=USER_ADMIN, role="admin", display_name="Operations admin"),
        User(id=USER_SUPPORT, role="support", display_name="Support desk"),
    ]
    db.add_all(users)
    db.flush()
    db.add(TreatingRelationship(reviewer_user_id=USER_TREATING, member_id=MEMBER_ALEX, ended_on=None))


def token_for(user_id: uuid.UUID, role: str, ttl_seconds: int = 7 * 24 * 3600) -> str:
    return issue_token(user_id, role, get_settings().token_secret, ttl_seconds)


def main() -> None:
    from app.db import SessionLocal, engine
    from app.models import Base

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed(db)
        db.commit()
    secret_note = "Tokens below are signed with PRIOR_AUTH_TOKEN_SECRET and last 7 days."
    rows = [
        ("Northshore office", "provider", USER_OFFICE_ME),
        ("Pacific office", "provider", USER_OFFICE_HI),
        ("Alex Rivera (member)", "member", USER_ALEX),
        ("Riley Okonkwo (reviewer)", "reviewer", USER_REVIEWER),
        ("Jordan Blake (treats Alex)", "reviewer", USER_TREATING),
        ("Sam Patel (reviewer)", "reviewer", USER_REVIEWER_2),
        ("Operations admin", "admin", USER_ADMIN),
        ("Support desk", "support", USER_SUPPORT),
    ]
    print(secret_note)
    for label, role, user_id in rows:
        print(f"{label}\n  {token_for(user_id, role)}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
