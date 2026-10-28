import asyncio

import pytest

from api.services.agent_tools import execute_tool
from api.services.permission_broker import PUBLIC_READ, INTERNAL_READ, WRITE, classify_tool

pytestmark = pytest.mark.unit


def run_tool(name, payload):
    return asyncio.run(execute_tool(name, payload))


def test_calculator_is_deterministic_and_public():
    assert run_tool("calculate", {"expression": "(10 + 5) * 3"}) == "(10 + 5) * 3 = 45"
    assert classify_tool("calculate", {"expression": "2+2"}).category == PUBLIC_READ


def test_calculator_rejects_code_execution():
    result = run_tool("calculate", {"expression": "__import__('os').system('echo bad')"})
    assert result.startswith("Error:")


def test_current_time_is_public_and_validates_timezone():
    result = run_tool("current_time", {"timezone": "Pacific/Auckland"})
    assert result.startswith("Current time in Pacific/Auckland:")
    assert classify_tool("current_time", {}).category == PUBLIC_READ
    assert run_tool("current_time", {"timezone": "Not/AZone"}).startswith("Error:")


def test_personal_state_read_is_internal_and_write_requires_permission():
    assert classify_tool("get_personal_state", {}).category == INTERNAL_READ
    assert classify_tool(
        "update_personal_state", {"section": "goals", "values": {"x": 1}}
    ).category == WRITE
