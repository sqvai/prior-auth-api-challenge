# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Who may do what.

Auditors are the next group the UM team named. They are not a role yet.
An unknown role matches nothing here, so it sees no rows and can take no action.
Add the role in both `can_see_request` and `list_clause` in the same change.
"""

from sqlalchemy import false, or_

from app.models import AuthRequest, User

MEMBER = "member"
PROVIDER = "provider"
REVIEWER = "reviewer"
ADMIN = "admin"
SUPPORT = "support"
ROLES = frozenset({MEMBER, PROVIDER, REVIEWER, ADMIN, SUPPORT})

TERMINAL = frozenset({"approved", "denied", "partial"})
DENIAL_REASON_CODES = frozenset(
    {
        "medical_necessity",
        "not_a_covered_benefit",
        "insufficient_documentation",
        "experimental",
        "needs_a_different_code",
        "other",
    }
)


def can_see_request(actor: User, request: AuthRequest) -> bool:
    if actor.role in (ADMIN, SUPPORT):
        return True
    if actor.role == MEMBER:
        return request.member_id == actor.member_id
    if actor.role == PROVIDER:
        return request.practice_id == actor.practice_id
    if actor.role == REVIEWER:
        return request.status == "submitted" or request.reviewer_id == actor.id
    return False


def list_clause(actor: User):
    """SQL restriction for list queries. None means every request."""
    if actor.role in (ADMIN, SUPPORT):
        return None
    if actor.role == MEMBER:
        return AuthRequest.member_id == actor.member_id
    if actor.role == PROVIDER:
        return AuthRequest.practice_id == actor.practice_id
    if actor.role == REVIEWER:
        return or_(AuthRequest.status == "submitted", AuthRequest.reviewer_id == actor.id)
    return false()
