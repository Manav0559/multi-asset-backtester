"""Test fixtures. Tests run against the docker-compose TimescaleDB —
each test uses uniquely-suffixed users and cleans up after itself, so
the suite is safe to run repeatedly against a dev database."""
import os

# Must be set BEFORE app.core.config is imported (below) so Settings() picks it
# up: the whole suite fires from one client IP within a single 60s window and
# would trip the per-IP rate limiter. Throttling is prod behavior, not test.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import User

_CREATED_EMAILS: list[str] = []


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def unique():
    """Unique suffix per test so parallel/repeat runs never collide."""
    suffix = uuid.uuid4().hex[:10]
    email = f"test_{suffix}@example.com"
    _CREATED_EMAILS.append(email)
    return {"suffix": suffix, "email": email, "username": f"user_{suffix}",
            "password": "s3cret-pass!"}


@pytest.fixture(autouse=True, scope="session")
def _cleanup_after_session():
    yield
    with SessionLocal() as db:
        if _CREATED_EMAILS:
            db.execute(delete(User).where(User.email.in_(_CREATED_EMAILS)))
            db.commit()
