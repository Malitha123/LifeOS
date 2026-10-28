"""Deterministic routing for the Personal AI Chief of Staff.

This module does not execute tools and never grants permissions.  It produces a
small execution plan that the primary assistant can use to decide whether a
request is ordinary reasoning, a public/direct-tool request, a single-domain
request, or a cross-domain decision.

The router is intentionally deterministic and fail-soft.  The main agent still
has the final reasoning role, while the structural PermissionBroker remains the
only authority for protected data access and side effects.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable


EXECUTION_GENERAL = "general"
EXECUTION_DIRECT = "direct_tool"
EXECUTION_SINGLE = "single_agent"
EXECUTION_MULTI = "multi_agent"

DOMAINS = (
    "career",
    "home",
    "travel",
    "money",
    "fitness",
    "learning",
    "life_admin",
    "network",
    "research",
    "planning",
)

_DOMAIN_PATTERNS: dict[str, tuple[str, ...]] = {
    "career": (
        r"\bjob(?:s)?\b", r"\bcareer\b", r"\binterview(?:s)?\b", r"\brecruiter(?:s)?\b",
        r"\bcv\b", r"\bresume\b", r"\bsalary\b", r"\boffer\b", r"\bemployer\b",
        r"\bapplication(?:s)?\b", r"\bpromotion\b", r"\bwork role\b",
    ),
    "home": (
        r"\bapartment(?:s)?\b", r"\bflat(?:s)?\b", r"\brent(?:al|ing|s)?\b",
        r"\blandlord\b", r"\bmove house\b", r"\bmove to\b", r"\bhousing\b",
        r"\bcommute\b", r"\bproperty\b",
    ),
    "travel": (
        r"\btravel\b", r"\btrip\b", r"\bflight(?:s)?\b", r"\bhotel(?:s)?\b",
        r"\bvisa\b", r"\bitinerary\b", r"\bholiday\b", r"\bvacation\b",
        r"\bdestination(?:s)?\b", r"\bairfare\b",
    ),
    "money": (
        r"\bbudget\b", r"\bafford\b", r"\bfinance(?:s|ial)?\b", r"\bsaving(?:s)?\b",
        r"\binvest(?:ment|ments|ing)?\b", r"\bexpense(?:s)?\b", r"\bincome\b",
        r"\bcashflow\b", r"\bnet worth\b", r"\bcost of living\b",
    ),
    "fitness": (
        r"\bfitness\b", r"\bworkout(?:s)?\b", r"\bgym\b", r"\brun(?:ning)?\b",
        r"\bhyrox\b", r"\bstrength\b", r"\btraining\b", r"\brecovery\b",
        r"\bhike\b", r"\bhiking\b",
    ),
    "learning": (
        r"\blearn(?:ing)?\b", r"\bcourse(?:s)?\b", r"\bstudy\b", r"\bskill gap\b",
        r"\bcertification\b", r"\bpractice\b", r"\btutor\b",
    ),
    "life_admin": (
        r"\brenew(?:al)?\b", r"\bpassport\b", r"\binsurance\b", r"\bappointment\b",
        r"\bsubscription\b", r"\bbill(?:s)?\b", r"\bdeadline\b", r"\bform(?:s)?\b",
        r"\bvisa application\b", r"\badmin\b",
    ),
    "network": (
        r"\bnetworking\b", r"\bcontact(?:s)?\b", r"\bfollow[- ]?up\b", r"\bmentor\b",
        r"\bfriend(?:s)?\b", r"\brelationship(?:s)?\b", r"\breconnect\b",
    ),
    "research": (
        r"\bresearch\b", r"\bcompare\b", r"\bfind me\b", r"\blook up\b",
        r"\bsearch for\b", r"\bbest\b", r"\breview(?:s)?\b",
    ),
    "planning": (
        r"\bplan\b", r"\bprioriti[sz]e\b", r"\bschedule\b", r"\bwhat should i do\b",
        r"\bthis week\b", r"\btomorrow\b", r"\btoday\b", r"\broadmap\b",
    ),
}

_PUBLIC_TOOL_PATTERNS: dict[str, tuple[str, ...]] = {
    "calculator": (
        r"^\s*(?:what(?:'s| is)\s+)?[-+*/().\d\s%^]+\??\s*$",
        r"\bcalculate\b", r"\bpercentage\b", r"\bpercent of\b",
    ),
    "time": (
        r"\bwhat time\b", r"\bcurrent time\b", r"\btime in\b", r"\btimezone\b",
    ),
    "web": (
        r"\bweather\b", r"\bforecast\b", r"\bcurrent price\b", r"\blatest\b",
        r"\bnews\b", r"\bexchange rate\b", r"\bcurrency\b", r"\bopen now\b",
        r"\btoday(?:'s)? score\b",
    ),
}

_PROTECTED_PATTERNS: dict[str, tuple[str, ...]] = {
    "Gmail": (r"\bgmail\b", r"\bemail(?:s)?\b", r"\binbox\b"),
    "Google Calendar": (r"\bcalendar\b", r"\bmy meetings?\b", r"\bmy schedule\b"),
    "Google Drive": (r"\bgoogle drive\b", r"\bmy drive\b", r"\bdrive file(?:s)?\b"),
    "GitHub / repository": (r"\bgithub\b", r"\brepositor(?:y|ies)\b", r"\bcommit\b", r"\bpush\b", r"\bpull request\b"),
    "Finance": (r"\bmy bank\b", r"\bmy transactions\b", r"\bmy finances\b", r"\bmonarch\b"),
    "Slack": (r"\bslack\b", r"\bdm(?:s)?\b"),
    "Messages": (r"\bmy texts?\b", r"\bimessage\b", r"\bwhatsapp\b"),
}

_ACTION_RE = re.compile(
    r"\b(send|create|add|update|edit|change|delete|remove|cancel|book|buy|purchase|"
    r"pay|submit|post|publish|push|merge|reply|forward|schedule|reschedule|save|remember|log)\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"\b(should i|which (?:one|option)|better choice|worth it|recommend|decide|decision|trade[- ]?off)\b",
    re.IGNORECASE,
)
_HIGH_IMPACT_RE = re.compile(
    r"\b(send|delete|cancel|buy|purchase|pay|book|submit|publish|push|merge|transfer|accept offer|decline offer)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PersonalRoutePlan:
    execution_mode: str
    domains: tuple[str, ...] = ()
    public_tools: tuple[str, ...] = ()
    protected_services: tuple[str, ...] = ()
    action_required: bool = False
    review_required: bool = False
    reasoning: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _matches(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def route_personal_request(query: str) -> PersonalRoutePlan:
    """Return a conservative execution plan for a user request.

    This function never decides that protected access is authorized.  It only
    reports that a request *appears* to involve a protected service so the
    assistant can expect the PermissionBroker to stop that tool call.
    """
    text = (query or "").strip()
    lower = text.lower()

    domains = tuple(
        domain for domain, patterns in _DOMAIN_PATTERNS.items()
        if _matches(lower, patterns)
    )
    public_tools = tuple(
        tool for tool, patterns in _PUBLIC_TOOL_PATTERNS.items()
        if _matches(lower, patterns)
    )
    protected_services = tuple(
        service for service, patterns in _PROTECTED_PATTERNS.items()
        if _matches(lower, patterns)
    )
    action_required = bool(_ACTION_RE.search(lower))
    decision = bool(_DECISION_RE.search(lower))
    review_required = bool(_HIGH_IMPACT_RE.search(lower)) or (decision and len(domains) >= 2)

    # Research/planning are cross-cutting helper lenses, not usually enough by
    # themselves to make a request a domain-specialist request.
    substantive_domains = tuple(d for d in domains if d not in {"research", "planning"})

    if len(substantive_domains) >= 2:
        execution_mode = EXECUTION_MULTI
        reason = "Request spans multiple life domains."
    elif len(substantive_domains) == 1:
        execution_mode = EXECUTION_SINGLE
        reason = f"Request is primarily a {substantive_domains[0]} question."
    elif public_tools:
        execution_mode = EXECUTION_DIRECT
        reason = "Request can be handled with a public/direct utility path."
    elif domains:
        # planning/research alone still benefit from one specialist lens.
        execution_mode = EXECUTION_SINGLE
        reason = f"Request is primarily a {domains[0]} task."
    else:
        execution_mode = EXECUTION_GENERAL
        reason = "No specialist domain or current-data tool is required by the wording."

    # Explicit personal protected-source questions stay agentic even when no
    # life-domain keyword is present, because a tool decision is required and
    # the permission broker must mediate access.
    if protected_services and execution_mode == EXECUTION_GENERAL:
        execution_mode = EXECUTION_SINGLE
        reason = "Request needs protected personal-service access."
        domains = ("life_admin",)

    return PersonalRoutePlan(
        execution_mode=execution_mode,
        domains=domains,
        public_tools=public_tools,
        protected_services=protected_services,
        action_required=action_required,
        review_required=review_required,
        reasoning=reason,
    )
