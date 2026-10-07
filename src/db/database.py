# Engine initialization (Postgres / SQLite fallback)

from sqlmodel import create_engine, Session
from sqlalchemy import event
from src.config import settings

# SQLite requires extra connection arguments for multi-thread support in FastAPI
connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(settings.DATABASE_URL, echo=False, connect_args=connect_args)

# Enable foreign key enforcement on SQLite for every connection
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if settings.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

# Alembic owns the schema exclusively. SQLModel.metadata.create_all is not used
# anywhere in the application: it issues CREATE TABLE IF NOT EXISTS, so against a
# migrated database it silently no-ops and would mask a missing migration.

def get_session():
    """Dependency for providing a transactional database session.

    The caller owns the transaction, so ledger writes and the actions they
    authorise can be committed or rolled back together (fail-closed, A3).
    """
    with Session(engine) as session:
        yield session