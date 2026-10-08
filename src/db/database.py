# Engine initialization (Postgres / SQLite fallback)

from sqlmodel import create_engine, Session
from sqlalchemy import event
from sqlalchemy.engine import make_url
from src.config import settings

database_url = make_url(settings.DATABASE_URL)
is_sqlite = database_url.get_backend_name() == "sqlite"
is_postgresql = database_url.get_backend_name() == "postgresql"

# Check pooled connections before use. This cannot recover a transaction whose
# connection drops midway; callers still roll back and report the failure.
engine_options = {"pool_pre_ping": True} if is_postgresql else {}
connect_args = {"check_same_thread": False} if is_sqlite else {}
if is_postgresql and database_url.get_driver_name() == "psycopg2":
    # libpq options: bound connection establishment and detect dead TCP peers.
    # These are transport settings, not SQL query or transaction timeouts.
    connect_args.update(
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )

engine = create_engine(settings.DATABASE_URL, echo=False,
                       connect_args=connect_args, **engine_options)

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
