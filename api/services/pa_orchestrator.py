"""Lightweight multi-agent advisory layer for the Personal Chief of Staff.

Specialists run in parallel on the routing-tier model and return short internal
briefs. They receive the user's question, not protected connector data, and have
no tools. The primary agent remains responsible for evidence gathering,
permissions, and the final answer.
"""
from __future__ import annotations

import asyncio
import logging

from api.services.llm_client import generate_text
from api.services.pa_router import EXECUTION_MULTI, PersonalRoutePlan
from api.services.pa_specialists import SPECIALISTS

logger = logging.getLogger(__name__)

_MAX_SPECIALISTS = 4
_MAX_BRIEF_CHARS = 1800


async def _consult(domain: str, question: str) -> tuple[str, str]:
    guidance = SPECIALISTS.get(domain, "")
    prompt = f"""You are an internal {domain} specialist supporting a Personal AI Chief of Staff.

{guidance}

User request:
{question}

Return a concise internal brief, not a user-facing response. Include:
1. the most important factors or recommendation from your domain,
2. facts/data that should be verified before a confident conclusion,
3. major risks or trade-offs.

You have no tools and no permission to assume private data. Never claim that Gmail, Calendar, GitHub, Drive, finance, messages, or another protected source was checked. Keep the brief under 180 words."""
    text = await generate_text(prompt, max_tokens=500, temperature=0.2, timeout=12)
    return domain, (text or "").strip()[:_MAX_BRIEF_CHARS]


async def _review(question: str, briefs: list[tuple[str, str]]) -> str:
    joined = "\n\n".join(f"[{domain}]\n{text}" for domain, text in briefs if text)
    if not joined:
        return ""
    prompt = f"""You are an internal reviewer for a Personal AI Chief of Staff.

User request:
{question}

Specialist briefs:
{joined}

Identify only material conflicts, unsupported assumptions, missing current facts, calculation risks, or irreversible consequences the final Chief of Staff answer must handle. Do not answer the user and do not assume access to private services. Keep this under 140 words."""
    text = await generate_text(prompt, max_tokens=400, temperature=0.1, timeout=12)
    return (text or "").strip()[:_MAX_BRIEF_CHARS]


async def consult_specialists(plan: PersonalRoutePlan, question: str) -> str:
    """Return an internal multi-agent briefing block, or ``""`` on degradation.

    Failure is intentionally non-fatal. The normal Chief-of-Staff agent can
    still answer using static specialist guidance and its normal tool loop.
    """
    if plan.execution_mode != EXECUTION_MULTI:
        return ""

    domains = [d for d in plan.domains if d in SPECIALISTS and d not in {"research", "planning"}]
    if not domains:
        return ""
    domains = domains[:_MAX_SPECIALISTS]

    results = await asyncio.gather(
        *[_consult(domain, question) for domain in domains],
        return_exceptions=True,
    )
    briefs: list[tuple[str, str]] = []
    for domain, result in zip(domains, results):
        if isinstance(result, Exception):
            logger.debug("PA specialist %s unavailable: %s", domain, result)
            continue
        _, text = result
        if text:
            briefs.append((domain, text))
    if not briefs:
        return ""

    reviewer = ""
    if plan.review_required:
        try:
            reviewer = await _review(question, briefs)
        except Exception as exc:  # fail-soft, never block the main assistant
            logger.debug("PA reviewer unavailable: %s", exc)

    lines = [
        "## Internal multi-agent advisory briefs",
        "These are advisory only. Verify changing facts with tools, obey the PermissionBroker, and give the user one synthesized answer.",
    ]
    for domain, text in briefs:
        lines.append(f"### {domain}\n{text}")
    if reviewer:
        lines.append(f"### reviewer\n{reviewer}")
    return "\n\n".join(lines)
