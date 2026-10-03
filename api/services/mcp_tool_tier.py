"""Tool tier for OAuth-authenticated MCP HTTP requests ("read + safe writes").

`OAUTH_TOOL_TIER` classifies every tool the MCP server can build — each
curated tool and each `lifeos_agent_*` tool — as allowed or denied, with a
reason. Only the names marked allowed are listed to, or callable by, an
OAuth-authenticated client; the fixed bearer-token transport is unaffected.
A tool missing from this table is denied, and `tests/test_mcp_oauth.py`
fails until it is classified here.

Allowed writes carry argument guards (`check_oauth_arguments`) so a safe
write cannot be turned into an execution path: a task cannot be handed to an
agent engine, and a reminder cannot run a prompt or call an endpoint when it
fires.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from api.services.agent_board import AGENT_PICKUP_TAGS, HUMAN_TAG, PROTECTED_TAGS


@dataclass(frozen=True)
class ToolClass:
    allowed: bool
    reason: str
    read_only: bool = False


def _read(reason: str = "read-only") -> ToolClass:
    return ToolClass(allowed=True, reason=reason, read_only=True)


def _safe_write(reason: str) -> ToolClass:
    return ToolClass(allowed=True, reason=reason, read_only=False)


def _deny(reason: str) -> ToolClass:
    return ToolClass(allowed=False, reason=reason)


_AGENT_DENY = "inter-agent family: agent spawning, messaging and session control"

OAUTH_TOOL_TIER: dict[str, ToolClass] = {
    # Reads
    "lifeos_ask": _read("vault question answering; reads and synthesizes, no side effects"),
    "lifeos_search": _read("vault search"),
    "lifeos_calendar_upcoming": _read(),
    "lifeos_calendar_search": _read(),
    "lifeos_gmail_search": _read(),
    "lifeos_drive_search": _read(),
    "lifeos_vault_list": _read(),
    "lifeos_conversations_list": _read(),
    "lifeos_memories_search": _read(),
    "lifeos_people_search": _read(),
    "lifeos_health": _read(),
    "lifeos_imessage_search": _read(),
    "lifeos_slack_search": _read(),
    "lifeos_slack_my_messages": _read(),
    "lifeos_person_facts": _read(),
    "lifeos_person_profile": _read(),
    "lifeos_person_timeline": _read(),
    "lifeos_meeting_prep": _read(),
    "lifeos_communication_gaps": _read(),
    "lifeos_person_connections": _read(),
    "lifeos_relationship_insights": _read(),
    "lifeos_photos_person": _read(),
    "lifeos_photos_shared": _read(),
    "lifeos_photos_stats": _read(),
    "lifeos_reminder_list": _read(),
    "lifeos_schedule_list": _read(),
    "lifeos_monarch_accounts": _read(),
    "lifeos_monarch_transactions": _read(),
    "lifeos_monarch_cashflow": _read(),
    "lifeos_monarch_budgets": _read(),
    "lifeos_investments": _read(),
    "lifeos_task_list": _read(),
    "lifeos_task_children": _read(),
    "lifeos_human_queue_list": _read(),
    "lifeos_turn_context": _read(),
    "lifeos_home_eero_status": _read("lists household network targets; no control"),
    # ChatGPT-shaped wrappers, registered only for OAuth requests
    "search": _read("ChatGPT search wrapper over vault search"),
    "fetch": _read("ChatGPT fetch of a document a search returned"),
    # Safe writes
    "lifeos_task_create": _safe_write(
        "creates a task; engine, consent, protected and #human tags and the fields map are refused"
    ),
    "lifeos_reminder_create": _safe_write(
        "creates a static Telegram reminder to the operator; prompt and endpoint reminders are refused"
    ),
    "lifeos_memories_create": _safe_write("saves a memory"),
    "lifeos_gmail_draft": _safe_write("creates an unsent Gmail draft"),
    # Denied
    "lifeos_vault_write": _deny("writes or overwrites vault files"),
    "lifeos_person_update": _deny("person edit"),
    "lifeos_person_fact_update": _deny("fact edit"),
    "lifeos_person_fact_confirm": _deny("fact edit"),
    "lifeos_person_fact_delete": _deny("delete"),
    "lifeos_gmail_send": _deny("sends email"),
    "lifeos_telegram_send": _deny("sends a Telegram message"),
    "lifeos_reminder_update": _deny("reminder edit could switch a reminder to a prompt or endpoint action"),
    "lifeos_reminder_delete": _deny("delete"),
    "lifeos_schedule_create": _deny("schedules can run prompts, endpoints and agents"),
    "lifeos_schedule_update": _deny("schedules can run prompts, endpoints and agents"),
    "lifeos_schedule_delete": _deny("delete"),
    "lifeos_schedule_trigger": _deny("schedule trigger"),
    "lifeos_sync_trigger": _deny("sync trigger"),
    "lifeos_workout_manage": _deny("mixes logging writes and profile edits behind one action argument"),
    "lifeos_task_update": _deny("task edit can assign an agent engine"),
    "lifeos_task_complete": _deny("task lifecycle change"),
    "lifeos_project_start": _deny("project lifecycle and agent execution"),
    "lifeos_project_complete": _deny("project lifecycle"),
    "lifeos_project_plan": _deny("starts agent-owner planning and delegation"),
    "lifeos_project_cancel": _deny("project cancellation"),
    "lifeos_project_pause": _deny("project lifecycle"),
    "lifeos_project_resume": _deny("project lifecycle and agent execution"),
    "lifeos_task_resume_execution": _deny("resumes automatic agent execution"),
    "lifeos_task_delete": _deny("delete"),
    "lifeos_human_queue_add": _deny("files operator work cards; not in the safe-write set"),
    "lifeos_human_queue_resolve": _deny("closes operator work cards"),
    "lifeos_calendar_create": _deny("sends calendar invitations to attendees"),
    "lifeos_calendar_update": _deny("sends calendar update emails"),
    "lifeos_calendar_delete": _deny("delete; sends cancellation emails"),
    "lifeos_home_eero_pause": _deny("home-network control"),
    "lifeos_home_eero_resume": _deny("home-network control"),
    "lifeos_agent_project_handoff": _deny(_AGENT_DENY),
    "lifeos_agent_project_owner": _deny(_AGENT_DENY),
    "lifeos_agent_spawn": _deny(_AGENT_DENY),
    "lifeos_agent_send": _deny(_AGENT_DENY),
    "lifeos_agent_check": _deny(_AGENT_DENY),
    "lifeos_agent_yield_until": _deny(_AGENT_DENY),
    "lifeos_agent_kill": _deny(_AGENT_DENY),
    "lifeos_agent_transcript_read": _deny(_AGENT_DENY),
    "lifeos_agent_sessions_list": _deny(_AGENT_DENY),
    "lifeos_agent_user_ask": _deny(_AGENT_DENY),
    "lifeos_agent_execution_override": _deny(_AGENT_DENY),
}

OAUTH_ALLOWED_TOOLS: frozenset[str] = frozenset(
    name for name, cls in OAUTH_TOOL_TIER.items() if cls.allowed
)


def tool_annotations(name: str) -> dict:
    """MCP tool annotations for an allowed tool, so apps can confirm writes."""
    cls = OAUTH_TOOL_TIER[name]
    if cls.read_only:
        return {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
    return {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }


# Tags that hand a task to an agent engine, record execution state, or file
# it for the operator. Normalized as `agent_board.normalize_tags` does.
_FORBIDDEN_TASK_TAGS = frozenset(
    t.lower() for t in (*AGENT_PICKUP_TAGS, *PROTECTED_TAGS, HUMAN_TAG)
)
_TASK_CREATE_KEYS = frozenset(
    {"description", "context", "status", "priority", "due_date", "tags", "notes", "operation_key"}
)
_INLINE_TAG_RE = re.compile(r"#([\w-]+)")


def _forbidden_tag(tag: str) -> bool:
    normalized = tag.strip().lstrip("#").lower()
    return normalized in _FORBIDDEN_TASK_TAGS or normalized.startswith("agent")


def _check_task_create(arguments: dict) -> str | None:
    extra = sorted(set(arguments) - _TASK_CREATE_KEYS)
    if extra:
        return f"arguments not available to connected apps: {', '.join(extra)}"
    tags = arguments.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return "tags must be a list of strings"
        if any(_forbidden_tag(t) for t in tags):
            return "engine, execution and #human tags cannot be set by connected apps"
    for key in ("description", "context", "notes", "status", "priority", "due_date", "operation_key"):
        value = arguments.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return f"{key} must be a string"
        if any(_forbidden_tag(t) for t in _INLINE_TAG_RE.findall(value)):
            return "engine, execution and #human tags cannot be set by connected apps"
    return None


def _check_reminder_create(arguments: dict) -> str | None:
    if arguments.get("message_type") != "static":
        return "connected apps can create only static reminders (message_type='static')"
    if arguments.get("endpoint_config") is not None:
        return "endpoint_config is not available to connected apps"
    return None


_ARGUMENT_GUARDS = {
    "lifeos_task_create": _check_task_create,
    "lifeos_reminder_create": _check_reminder_create,
}


def check_oauth_arguments(name: str, arguments: dict) -> str | None:
    """Return a refusal message when an allowed tool's arguments leave the tier."""
    guard = _ARGUMENT_GUARDS.get(name)
    return guard(arguments) if guard else None
