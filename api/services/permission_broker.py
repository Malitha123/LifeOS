"""Central permission broker for Personal Assistant tool access.

The broker is deliberately fail-closed. Public web/local read tools are
explicitly allow-listed. Private external reads and every state-changing
operation require an explicit, one-use approval from the user before the tool
handler is allowed to run.

Approvals are scoped to the exact conversation + tool + normalized arguments
that triggered the request. A grant is consumed on first successful use and
cannot be reused by another call, turn, conversation, or tool.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Iterable


PUBLIC_READ = "public_read"
INTERNAL_READ = "internal_read"
PRIVATE_READ = "private_read"
WRITE = "write"
DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class PermissionPolicy:
    category: str
    service: str
    operation: str
    reason: str

    @property
    def requires_permission(self) -> bool:
        return self.category not in {PUBLIC_READ, INTERNAL_READ}


@dataclass
class PermissionRequest:
    id: str
    conversation_id: str
    tool_name: str
    input_fingerprint: str
    service: str
    operation: str
    category: str
    reason: str
    scope: str
    status: str
    created_at: float
    expires_at: float
    approved_at: float | None = None
    denied_at: float | None = None
    consumed_at: float | None = None

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # The fingerprint is an internal binding detail, not useful to the UI.
        data.pop("input_fingerprint", None)
        return data


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    policy: PermissionPolicy
    request: PermissionRequest | None = None
    consumed_grant_id: str | None = None


# Tools whose behavior is unambiguously read-only and local/public.
# Everything not explicitly classified below fails closed.
_ALWAYS_ALLOWED: dict[str, PermissionPolicy] = {
    "search_web": PermissionPolicy(PUBLIC_READ, "Public web", "search", "Search public web information."),
    "search_vault": PermissionPolicy(INTERNAL_READ, "Personal Lake", "read", "Search already-ingested local notes."),
    "read_vault_file": PermissionPolicy(INTERNAL_READ, "Personal Lake", "read", "Read an already-ingested local note."),
    "list_vault": PermissionPolicy(INTERNAL_READ, "Personal Lake", "read", "List already-ingested local notes."),
    "search_memories": PermissionPolicy(INTERNAL_READ, "PA memory", "read", "Search local assistant memory."),
    "person_info": PermissionPolicy(INTERNAL_READ, "Personal Lake", "read", "Read locally indexed person context."),
}

# Protected reads. Even though some data may be cached locally, these names
# represent access to private life domains and are intentionally permissioned.
_PRIVATE_READS: dict[str, PermissionPolicy] = {
    "search_calendar": PermissionPolicy(PRIVATE_READ, "Google Calendar", "read", "Read private calendar data."),
    "search_email": PermissionPolicy(PRIVATE_READ, "Gmail", "read", "Read private email data."),
    "search_drive": PermissionPolicy(PRIVATE_READ, "Google Drive", "read", "Read private Drive data."),
    "search_slack": PermissionPolicy(PRIVATE_READ, "Slack", "read", "Read private Slack data."),
    "get_message_history": PermissionPolicy(PRIVATE_READ, "Messages", "read", "Read private message history."),
    "search_finances": PermissionPolicy(PRIVATE_READ, "Finance", "read", "Read private financial data."),
    "internet_status": PermissionPolicy(PRIVATE_READ, "Home network", "read", "Read private home-network state."),
}

# Unconditional state-changing tools.
_WRITES: dict[str, PermissionPolicy] = {
    "create_email_draft": PermissionPolicy(WRITE, "Gmail", "create draft", "Create an external Gmail draft."),
    "send_email_draft": PermissionPolicy(DESTRUCTIVE, "Gmail", "send email", "Send an email to an external recipient."),
    "create_calendar_event": PermissionPolicy(WRITE, "Google Calendar", "create event", "Create a calendar event and potentially send invitations."),
    "update_calendar_event": PermissionPolicy(WRITE, "Google Calendar", "update event", "Change a calendar event and potentially notify attendees."),
    "delete_calendar_event": PermissionPolicy(DESTRUCTIVE, "Google Calendar", "delete event", "Delete a calendar event and potentially send cancellations."),
    "save_memory": PermissionPolicy(WRITE, "PA memory", "save memory", "Persist a new long-term personal memory."),
    "pause_internet": PermissionPolicy(DESTRUCTIVE, "Home network", "pause internet", "Change household network access."),
    "resume_internet": PermissionPolicy(WRITE, "Home network", "resume internet", "Change household network access."),
    "repository_agent": PermissionPolicy(WRITE, "GitHub / repository", "allow repository-capable agent", "Allow a CLI agent to access the repository for this request."),
}

# Consolidated tools have read and write actions behind one tool name.
_READ_ACTIONS: dict[str, frozenset[str]] = {
    "manage_tasks": frozenset({"list", "children", "tags", "get"}),
    "manage_human_queue": frozenset({"list", "get"}),
    "manage_reminders": frozenset({"list", "get"}),
    "manage_schedules": frozenset({"list", "get"}),
    "manage_workouts": frozenset({"list", "history", "summary", "metrics", "get_profile", "readiness"}),
}

_SERVICE_FOR_CONSOLIDATED = {
    "manage_tasks": "Tasks",
    "manage_human_queue": "Human queue",
    "manage_reminders": "Reminders",
    "manage_schedules": "Scheduler",
    "manage_workouts": "Fitness",
}

_DESTRUCTIVE_ACTIONS = frozenset({
    "delete", "remove", "cancel", "complete", "send", "trigger", "pause", "kill",
})


def _normalized_payload(tool_input: dict[str, Any] | None) -> str:
    try:
        return json.dumps(tool_input or {}, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return repr(tool_input or {})


def input_fingerprint(tool_name: str, tool_input: dict[str, Any] | None) -> str:
    payload = f"{tool_name}\n{_normalized_payload(tool_input)}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _scope_for(tool_name: str, tool_input: dict[str, Any] | None) -> str:
    """Short, non-secret summary for the approval UI.

    Do not echo full email bodies, notes, or other potentially sensitive values.
    The user needs enough information to understand the operation, not a dump of
    the tool payload into logs/UI.
    """
    inp = tool_input or {}
    if tool_name == "search_email":
        parts = []
        for key, label in (("from_email", "from"), ("to_email", "to"), ("keywords", "keywords")):
            value = inp.get(key)
            if value:
                text = str(value)
                if len(text) > 80:
                    text = text[:77] + "..."
                parts.append(f"{label}: {text}")
        return "; ".join(parts) or "Search email for this request"
    if tool_name == "search_calendar":
        q = str(inp.get("query") or inp.get("date_ref") or "upcoming events")
        return f"Calendar lookup: {q[:100]}"
    if tool_name == "search_drive":
        q = str(inp.get("query") or "requested files")
        return f"Drive search: {q[:100]}"
    if tool_name == "search_slack":
        q = str(inp.get("query") or inp.get("keywords") or "requested messages")
        return f"Slack search: {q[:100]}"
    if tool_name == "search_finances":
        return "Read financial information needed for this request"
    if tool_name == "get_message_history":
        return "Read private message history needed for this request"
    if tool_name in _READ_ACTIONS:
        action = str(inp.get("action") or "write")
        return f"{action} via {tool_name}"
    if tool_name in _WRITES:
        return _WRITES[tool_name].reason
    return f"Use protected capability: {tool_name}"


def classify_tool(tool_name: str, tool_input: dict[str, Any] | None = None) -> PermissionPolicy:
    """Return the permission policy for one native PA tool call.

    Unknown tools are protected by default. Adding a new tool therefore cannot
    accidentally create a permission bypass; it must be deliberately classified.
    """
    if tool_name in _ALWAYS_ALLOWED:
        return _ALWAYS_ALLOWED[tool_name]
    if tool_name in _PRIVATE_READS:
        return _PRIVATE_READS[tool_name]
    if tool_name in _WRITES:
        return _WRITES[tool_name]
    if tool_name in _READ_ACTIONS:
        action = str((tool_input or {}).get("action") or "").strip().lower()
        service = _SERVICE_FOR_CONSOLIDATED[tool_name]
        if action in _READ_ACTIONS[tool_name]:
            return PermissionPolicy(INTERNAL_READ, service, action or "read", f"Read {service.lower()} state.")
        category = DESTRUCTIVE if action in _DESTRUCTIVE_ACTIONS else WRITE
        return PermissionPolicy(category, service, action or "change", f"Change {service.lower()} state.")
    return PermissionPolicy(
        PRIVATE_READ,
        "Protected capability",
        "access",
        f"Tool '{tool_name}' has not been explicitly classified as safe.",
    )


class PermissionBroker:
    """In-memory, one-use approval broker.

    The broker intentionally does not persist approvals. A process restart loses
    them and therefore fails closed. This is preferable to silently carrying an
    old approval into a new process or user context.
    """

    def __init__(self, ttl_seconds: int = 10 * 60):
        self.ttl_seconds = ttl_seconds
        self._requests: dict[str, PermissionRequest] = {}
        self._lock = threading.RLock()

    def _now(self) -> float:
        return time.time()

    def _expire_locked(self, now: float) -> None:
        for req in self._requests.values():
            if req.status in {"pending", "approved"} and req.expires_at <= now:
                req.status = "expired"

    def get(self, request_id: str) -> PermissionRequest | None:
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            return self._requests.get(request_id)

    def pending_for_conversation(self, conversation_id: str) -> list[PermissionRequest]:
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            return [
                req for req in self._requests.values()
                if req.conversation_id == conversation_id and req.status == "pending"
            ]

    def _matching_pending_locked(
        self, conversation_id: str, tool_name: str, fingerprint: str,
    ) -> PermissionRequest | None:
        for req in self._requests.values():
            if (
                req.conversation_id == conversation_id
                and req.tool_name == tool_name
                and req.input_fingerprint == fingerprint
                and req.status == "pending"
            ):
                return req
        return None

    def request_permission(
        self,
        *,
        conversation_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        policy: PermissionPolicy | None = None,
    ) -> PermissionRequest:
        policy = policy or classify_tool(tool_name, tool_input)
        if not policy.requires_permission:
            raise ValueError(f"Tool {tool_name!r} does not require permission")
        fingerprint = input_fingerprint(tool_name, tool_input)
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            existing = self._matching_pending_locked(conversation_id, tool_name, fingerprint)
            if existing is not None:
                return existing
            req = PermissionRequest(
                id=uuid.uuid4().hex,
                conversation_id=conversation_id,
                tool_name=tool_name,
                input_fingerprint=fingerprint,
                service=policy.service,
                operation=policy.operation,
                category=policy.category,
                reason=policy.reason,
                scope=_scope_for(tool_name, tool_input),
                status="pending",
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._requests[req.id] = req
            return req

    def approve(self, request_id: str) -> PermissionRequest:
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            req = self._requests.get(request_id)
            if req is None:
                raise KeyError(request_id)
            if req.status != "pending":
                raise ValueError(f"permission request is {req.status}")
            req.status = "approved"
            req.approved_at = now
            return req

    def deny(self, request_id: str) -> PermissionRequest:
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            req = self._requests.get(request_id)
            if req is None:
                raise KeyError(request_id)
            if req.status not in {"pending", "approved"}:
                raise ValueError(f"permission request is {req.status}")
            req.status = "denied"
            req.denied_at = now
            return req

    def authorize_or_request(
        self,
        *,
        conversation_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        grant_ids: Iterable[str] | None = None,
    ) -> PermissionDecision:
        policy = classify_tool(tool_name, tool_input)
        if not policy.requires_permission:
            return PermissionDecision(True, policy)

        fingerprint = input_fingerprint(tool_name, tool_input)
        grant_ids = tuple(grant_ids or ())
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            for grant_id in grant_ids:
                req = self._requests.get(str(grant_id))
                if req is None:
                    continue
                if (
                    req.status == "approved"
                    and req.conversation_id == conversation_id
                    and req.tool_name == tool_name
                    and req.input_fingerprint == fingerprint
                    and req.expires_at > now
                ):
                    req.status = "consumed"
                    req.consumed_at = now
                    return PermissionDecision(True, policy, consumed_grant_id=req.id)

            pending = self._matching_pending_locked(conversation_id, tool_name, fingerprint)
            if pending is None:
                pending = PermissionRequest(
                    id=uuid.uuid4().hex,
                    conversation_id=conversation_id,
                    tool_name=tool_name,
                    input_fingerprint=fingerprint,
                    service=policy.service,
                    operation=policy.operation,
                    category=policy.category,
                    reason=policy.reason,
                    scope=_scope_for(tool_name, tool_input),
                    status="pending",
                    created_at=now,
                    expires_at=now + self.ttl_seconds,
                )
                self._requests[pending.id] = pending
            return PermissionDecision(False, policy, request=pending)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._requests.clear()


_BROKER = PermissionBroker()


def get_permission_broker() -> PermissionBroker:
    return _BROKER


def permission_prompt_text(requests: Iterable[PermissionRequest]) -> str:
    reqs = list(requests)
    if not reqs:
        return ""
    if len(reqs) == 1:
        req = reqs[0]
        return (
            f"I need your permission to {req.operation} using {req.service} for this request. "
            "Approve the permission request to continue, or deny it and I will not access that service."
        )
    services = ", ".join(dict.fromkeys(req.service for req in reqs))
    return (
        f"I need your permission to use these protected services for this request: {services}. "
        "Approve each permission request you want me to use, or deny any you do not want me to access."
    )
