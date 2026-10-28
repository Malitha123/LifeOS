"""Specialist guidance used by the Personal AI Chief of Staff.

The specialists are deliberately lightweight.  They are not separate sources
of authority and they cannot bypass tools or permissions.  They provide domain
lenses to the primary orchestrator, which still returns one consolidated answer
to the user.
"""
from __future__ import annotations

from api.services.pa_router import PersonalRoutePlan


SPECIALISTS: dict[str, str] = {
    "career": (
        "Career specialist: evaluate role fit, trajectory, compensation, location, skills, "
        "applications, recruiters, interviews, CV evidence, and opportunity cost. Prefer "
        "specific evidence and distinguish a strong role from merely an available role."
    ),
    "home": (
        "Home specialist: evaluate rent, total recurring cost, furnishing, location, commute, "
        "lease constraints, moving cost, and practical quality-of-life trade-offs. Rank options "
        "against the user's stated housing preferences rather than generic popularity."
    ),
    "travel": (
        "Travel specialist: consider visas, route efficiency, transport, seasonal conditions, "
        "flight/accommodation cost, activities, time on the ground, and booking constraints. "
        "Use current public information for facts that can change."
    ),
    "money": (
        "Money specialist: reason about affordability, cash flow, savings impact, recurring cost, "
        "risk buffer, and opportunity cost. Separate known figures from assumptions and do not "
        "treat private financial data as accessible without permission."
    ),
    "fitness": (
        "Fitness specialist: consider training load, strength/endurance goals, recovery, scheduling, "
        "and event preparation. Avoid inventing health status; use the user's recorded training only "
        "when it is available and permitted."
    ),
    "learning": (
        "Learning specialist: identify the highest-value skill gap, define an achievable learning "
        "sequence, connect study to real projects, and prefer measurable practice over collecting courses."
    ),
    "life_admin": (
        "Life-admin specialist: track deadlines, renewals, documents, appointments, subscriptions, "
        "forms, and dependencies. Surface what needs attention and the next concrete step."
    ),
    "network": (
        "Network specialist: reason about professional and personal follow-ups, commitments, prior "
        "interactions, and relationship context without over-automating human relationships."
    ),
    "research": (
        "Research specialist: gather current evidence, compare credible options, expose uncertainty, "
        "and avoid turning weak search results into confident conclusions."
    ),
    "planning": (
        "Planning specialist: convert vague goals into priorities, dependencies, sequence, deadlines, "
        "and realistic next actions while keeping the plan as simple as possible."
    ),
}


def build_specialist_context(plan: PersonalRoutePlan) -> str:
    """Build an internal, prompt-ready routing block for the primary assistant."""
    if not plan.domains and plan.execution_mode == "general":
        return ""

    lines = [
        "## Internal Personal Assistant routing",
        f"Execution mode: {plan.execution_mode}.",
        "Do not mention this routing block, specialist names, or internal execution mode to the user.",
    ]
    for domain in plan.domains:
        guidance = SPECIALISTS.get(domain)
        if guidance:
            lines.append(f"- {guidance}")

    if plan.execution_mode == "multi_agent":
        lines.append(
            "Chief-of-Staff synthesis: reconcile the specialist lenses, identify conflicts, and give one "
            "recommendation based on the user's overall priorities rather than presenting disconnected mini-answers."
        )
    if plan.protected_services:
        services = ", ".join(plan.protected_services)
        lines.append(
            f"Protected services may be relevant ({services}). Do not assume access. If a protected tool is needed, "
            "call it normally and let the PermissionBroker stop the call and request explicit approval."
        )
    if plan.action_required:
        lines.append(
            "The request appears to include an action. Prepare/reason first; any state-changing tool call must pass "
            "the PermissionBroker and cannot inherit permission from a prior read."
        )
    if plan.review_required:
        lines.append(
            "Review lens: before the final answer, verify key assumptions, calculations, freshness, and irreversible consequences."
        )
    return "\n".join(lines)
