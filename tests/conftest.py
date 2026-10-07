import pytest
from src.evaluation import demo_session


@pytest.fixture
def runtime_session():
    with demo_session() as session:
        yield session
