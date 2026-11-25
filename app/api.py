# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

import uuid

from fastapi import APIRouter, Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.ai import build_provider
from app.auth import current_user
from app.clock import Clock
from app.config import Settings, get_settings
from app.db import get_db
from app.models import User
from app.schemas import DecisionIn, ReassignIn, SubmitIn
from app.service import (
    decide,
    get_request,
    history,
    list_attempts,
    list_notifications,
    list_requests,
    pickup,
    reassign,
    reviewer_load,
    submit_request,
)

router = APIRouter(prefix="/v1")


def get_clock() -> Clock:
    return Clock()


def get_briefing_provider(settings: Settings = Depends(get_settings)):
    return build_provider(settings.ai_base_url, settings.ai_api_key, settings.ai_model)


def respond(result) -> JSONResponse:
    return JSONResponse(status_code=result.status_code, content=jsonable_encoder(result.body))


@router.get("/me")
def me(actor: User = Depends(current_user)) -> dict:
    return {
        "id": str(actor.id),
        "role": actor.role,
        "display_name": actor.display_name,
        "practice_id": None if actor.practice_id is None else str(actor.practice_id),
        "member_id": None if actor.member_id is None else str(actor.member_id),
    }


@router.post("/auth-requests")
def create_request(
    payload: SubmitIn,
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
    briefing_provider=Depends(get_briefing_provider),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return respond(submit_request(db, actor, payload, idempotency_key, clock, briefing_provider))


@router.get("/auth-requests")
def read_requests(
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return respond(list_requests(db, actor, status, limit, offset))


@router.get("/auth-requests/{request_id}")
def read_request(request_id: uuid.UUID, actor: User = Depends(current_user), db: Session = Depends(get_db)):
    return respond(get_request(db, actor, request_id))


@router.get("/auth-requests/{request_id}/history")
def read_history(request_id: uuid.UUID, actor: User = Depends(current_user), db: Session = Depends(get_db)):
    return respond(history(db, actor, request_id))


@router.post("/auth-requests/{request_id}/pickup")
def pick_up(
    request_id: uuid.UUID,
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    return respond(pickup(db, actor, request_id, clock))


@router.post("/auth-requests/{request_id}/decision")
def record_decision(
    request_id: uuid.UUID,
    payload: DecisionIn,
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    return respond(decide(db, actor, request_id, payload, clock))


@router.post("/admin/auth-requests/{request_id}/reassign")
def reassign_request(
    request_id: uuid.UUID,
    payload: ReassignIn,
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    return respond(reassign(db, actor, request_id, payload.reviewer_id, clock))


@router.get("/admin/reviewer-load")
def read_reviewer_load(actor: User = Depends(current_user), db: Session = Depends(get_db)):
    return respond(reviewer_load(db, actor))


@router.get("/admin/notifications")
def read_notifications(actor: User = Depends(current_user), db: Session = Depends(get_db)):
    return respond(list_notifications(db, actor))


@router.get("/support/attempts")
def read_attempts(
    actor: User = Depends(current_user),
    db: Session = Depends(get_db),
    attempt_id: uuid.UUID | None = None,
    practice_id: uuid.UUID | None = None,
    member_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return respond(
        list_attempts(
            db,
            actor,
            attempt_id=attempt_id,
            practice_id=practice_id,
            member_id=member_id,
            limit=limit,
            offset=offset,
        )
    )
