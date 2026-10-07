import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlmodel import SQLModel


def test_migrated_schema_matches_models_and_enforces_enums(runtime_session):
    session = runtime_session
    with session.bind.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), SQLModel.metadata) == []
        checks = inspect(connection).get_check_constraints("autonomy_state")
        assert any(check["name"] == "ck_autonomy_mode" for check in checks)
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO autonomy_state (agent_id, action_class, mode, streak, updated_at) VALUES ('x','read_file','INVALID',0,CURRENT_TIMESTAMP)"))
