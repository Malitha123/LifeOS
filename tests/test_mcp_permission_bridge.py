import pytest

pytestmark = pytest.mark.unit


def test_private_mcp_tools_are_permissioned():
    from mcp_server import _mcp_permission_policy

    assert _mcp_permission_policy("lifeos_gmail_search") is not None
    assert _mcp_permission_policy("lifeos_calendar_upcoming") is not None
    assert _mcp_permission_policy("lifeos_drive_search") is not None
    assert _mcp_permission_policy("lifeos_search") is None


def test_mcp_writes_are_permissioned():
    from mcp_server import _mcp_permission_policy

    assert _mcp_permission_policy("lifeos_gmail_draft").requires_permission
    assert _mcp_permission_policy("lifeos_gmail_send").requires_permission
    assert _mcp_permission_policy("lifeos_vault_write").requires_permission
    assert _mcp_permission_policy("lifeos_memories_create").requires_permission
