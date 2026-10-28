import pytest

from api.services.pa_orchestrator import consult_specialists
from api.services.pa_router import route_personal_request

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_multi_agent_consults_specialists_and_reviewer(monkeypatch):
    calls = []

    async def fake_generate(prompt, **kwargs):
        calls.append(prompt)
        if "internal reviewer" in prompt:
            return "Check moving costs and whether the salary figure is base or total compensation."
        if "career specialist" in prompt:
            return "Career brief"
        if "home specialist" in prompt:
            return "Home brief"
        return "Other brief"

    monkeypatch.setattr("api.services.pa_orchestrator.generate_text", fake_generate)
    plan = route_personal_request("Should I move to Wellington for this $150k job offer?")
    result = await consult_specialists(plan, "Should I move to Wellington for this $150k job offer?")
    assert "Career brief" in result
    assert "Home brief" in result
    assert "reviewer" in result
    assert any("internal reviewer" in call for call in calls)


@pytest.mark.asyncio
async def test_non_multi_agent_does_not_call_llm(monkeypatch):
    async def fail_generate(*args, **kwargs):
        raise AssertionError("generate_text should not be called")

    monkeypatch.setattr("api.services.pa_orchestrator.generate_text", fail_generate)
    plan = route_personal_request("Find me an apartment")
    assert await consult_specialists(plan, "Find me an apartment") == ""


@pytest.mark.asyncio
async def test_specialist_failure_degrades_cleanly(monkeypatch):
    async def fail_generate(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("api.services.pa_orchestrator.generate_text", fail_generate)
    plan = route_personal_request("Can I afford this apartment?")
    assert await consult_specialists(plan, "Can I afford this apartment?") == ""
