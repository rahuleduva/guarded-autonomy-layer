import pytest
from src.evaluation import run_scenario


@pytest.mark.parametrize("number", range(1, 11))
def test_required_scenario(number):
    report = run_scenario(number)
    assert report["passed"]
    if number == 8:
        assert set(report["outcomes"]) == {"ALLOWED", "DENIED", "ESCALATED", "PENDING_APPROVAL", "SHADOW_LOGGED"}
