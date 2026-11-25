# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.
"""Append-only record of who did what.

Detail payloads must stay free of clinical-note text and reviewer free text.
Those live on the request and line rows, which are access-controlled.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent

SUBMISSION_ACCEPTED = "submission_accepted"
SUBMISSION_REJECTED = "submission_rejected"
SUBMISSION_REPLAYED = "submission_replayed"
PICKED_UP = "picked_up"
PICKUP_REFUSED = "pickup_refused"
DECISION_REFUSED = "decision_refused"
REASSIGNED = "reassigned"
DECISION_RECORDED = "decision_recorded"
AI_BRIEFING_CREATED = "ai_briefing_created"
AI_OUTPUT_REJECTED = "ai_output_rejected"
AI_BRIEFING_FAILED = "ai_briefing_failed"
RECORD_VIEWED = "record_viewed"
RECORD_VIEW_RESTRICTED = "record_view_restricted"
SUPPORT_LOOKUP = "support_lookup"
NOTIFICATION_ENQUEUED = "notification_enqueued"


def record(
    db: Session,
    *,
    actor_id: uuid.UUID,
    action: str,
    request_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            request_id=request_id,
            attempt_id=attempt_id,
            detail=detail or {},
        )
    )
