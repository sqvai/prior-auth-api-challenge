# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

import os

os.environ["PRIOR_AUTH_DATABASE_URL"] = "postgresql+psycopg://priorauth:priorauth@127.0.0.1:54329/priorauth_test"
os.environ["PRIOR_AUTH_TOKEN_SECRET"] = "test-secret"
os.environ["PRIOR_AUTH_AI_BASE_URL"] = ""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import get_clock
from app.clock import FrozenClock
from app.config import get_settings
import app.models  # noqa: F401  -- registers tables on Base.metadata
from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

get_settings.cache_clear()

FROZEN = FrozenClock(datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc))


@pytest.fixture(scope="session", autouse=True)
def schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()


@pytest.fixture
def client():
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    with SessionLocal() as db:
        seed(db)
        db.commit()
    app.dependency_overrides[get_clock] = lambda: FROZEN
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
