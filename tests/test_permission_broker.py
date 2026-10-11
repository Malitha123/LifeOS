import pytest

from api.services.permission_broker import (
    DESTRUCTIVE,
    INTERNAL_READ,
    PRIVATE_READ,
    WRITE,
    PermissionBroker,
    classify_tool,
)


@pytest.mark.unit
def test_public_and_internal_reads_do_not_require_permission():
    for tool, payload in (
        ("search_web", {"query": "weather Auckland"}),
        ("search_vault", {"query": "project notes"}),
        ("search_memories", {"query": "housing"}),
        ("manage_tasks", {"action": "list"}),
    ):
        policy = classify_tool(tool, payload)
        assert policy.category == INTERNAL_READ or tool == "search_web"
        assert policy.requires_permission is False


@pytest.mark.unit
def test_private_reads_require_permission():
    expected = {
        "search_email": "Gmail",
        "search_calendar": "Google Calendar",
        "search_drive": "Google Drive",
        "search_slack": "Slack",
        "get_message_history": "Messages",
        "search_finances": "Finance",
    }
    for tool, service in expected.items():
        policy = classify_tool(tool, {})
        assert policy.category == PRIVATE_READ
        assert policy.service == service
        assert policy.requires_permission


@pytest.mark.unit
def test_state_changes_require_permission():
    assert classify_tool("save_memory", {"content": "x"}).category == WRITE
    assert classify_tool("manage_tasks", {"action": "create"}).category == WRITE
    assert classify_tool("manage_reminders", {"action": "delete"}).category == DESTRUCTIVE
    assert classify_tool("send_email_draft", {"draft_id": "d1"}).category == DESTRUCTIVE


@pytest.mark.unit
def test_unknown_tool_fails_closed():
    policy = classify_tool("future_unclassified_tool", {})
    assert policy.requires_permission
    assert policy.service == "Protected capability"


@pytest.mark.unit
def test_approval_is_exact_one_use_and_conversation_bound():
    broker = PermissionBroker(ttl_seconds=60)
    payload = {"keywords": "recruiter"}

    first = broker.authorize_or_request(
        conversation_id="conv-a",
        tool_name="search_email",
        tool_input=payload,
        grant_ids=[],
    )
    assert not first.allowed
    assert first.request is not None
    request_id = first.request.id

    approved = broker.approve(request_id)
    assert approved.status == "approved"

    wrong_conversation = broker.authorize_or_request(
        conversation_id="conv-b",
        tool_name="search_email",
        tool_input=payload,
        grant_ids=[request_id],
    )
    assert not wrong_conversation.allowed

    wrong_payload = broker.authorize_or_request(
        conversation_id="conv-a",
        tool_name="search_email",
        tool_input={"keywords": "different"},
        grant_ids=[request_id],
    )
    assert not wrong_payload.allowed

    allowed = broker.authorize_or_request(
        conversation_id="conv-a",
        tool_name="search_email",
        tool_input=payload,
        grant_ids=[request_id],
    )
    assert allowed.allowed
    assert allowed.consumed_grant_id == request_id
    assert broker.get(request_id).status == "consumed"

    replay = broker.authorize_or_request(
        conversation_id="conv-a",
        tool_name="search_email",
        tool_input=payload,
        grant_ids=[request_id],
    )
    assert not replay.allowed
    assert replay.request is not None
    assert replay.request.id != request_id


@pytest.mark.unit
def test_denied_request_never_authorizes():
    broker = PermissionBroker(ttl_seconds=60)
    decision = broker.authorize_or_request(
        conversation_id="conv-a",
        tool_name="search_calendar",
        tool_input={"query": "interview"},
    )
    request_id = decision.request.id
    broker.deny(request_id)

    blocked = broker.authorize_or_request(
        conversation_id="conv-a",
        tool_name="search_calendar",
        tool_input={"query": "interview"},
        grant_ids=[request_id],
    )
    assert not blocked.allowed
    assert broker.get(request_id).status == "denied"
