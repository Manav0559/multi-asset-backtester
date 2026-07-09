"""SQLAlchemy engine + session factory."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.metrics import DB_POOL_CHECKED_OUT, DB_POOL_SIZE

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Pool saturation is the leading indicator for every "the API got slow" page —
# export checked-out count + configured size so alerts can ratio them.
DB_POOL_SIZE.set(engine.pool.size())
event.listen(engine, "checkout", lambda *a: DB_POOL_CHECKED_OUT.inc())
event.listen(engine, "checkin", lambda *a: DB_POOL_CHECKED_OUT.dec())


def get_db():
    """FastAPI dependency that yields a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
