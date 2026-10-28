import pytest

from api.services.pa_router import (
    EXECUTION_DIRECT,
    EXECUTION_GENERAL,
    EXECUTION_MULTI,
    EXECUTION_SINGLE,
    route_personal_request,
)
from api.services.pa_specialists import build_specialist_context

pytestmark = pytest.mark.unit


def test_general_question_stays_general():
    plan = route_personal_request("Explain why the sky is blue")
    assert plan.execution_mode == EXECUTION_GENERAL
    assert plan.domains == ()


def test_weather_uses_direct_public_path():
    plan = route_personal_request("What's the weather forecast today?")
    assert plan.execution_mode == EXECUTION_DIRECT
    assert "web" in plan.public_tools
    assert not plan.protected_services


def test_apartment_search_uses_home_specialist():
    plan = route_personal_request("Find me suitable apartments near the CBD")
    assert plan.execution_mode == EXECUTION_SINGLE
    assert "home" in plan.domains
    assert "research" in plan.domains


def test_job_relocation_is_multi_domain():
    plan = route_personal_request("Should I move to Wellington for this $150k job offer?")
    assert plan.execution_mode == EXECUTION_MULTI
    assert "career" in plan.domains
    assert "home" in plan.domains
    assert plan.review_required


def test_affordability_combines_home_and_money():
    plan = route_personal_request("Can I afford this apartment at $600 a week?")
    assert plan.execution_mode == EXECUTION_MULTI
    assert "home" in plan.domains
    assert "money" in plan.domains


def test_email_access_is_marked_protected_but_not_authorized():
    plan = route_personal_request("Check my Gmail for a recruiter reply")
    assert "Gmail" in plan.protected_services
    assert plan.execution_mode == EXECUTION_SINGLE


def test_send_email_marks_action_and_review():
    plan = route_personal_request("Send an email to the recruiter saying Tuesday works")
    assert plan.action_required
    assert plan.review_required
    assert "Gmail" in plan.protected_services


def test_specialist_context_reminds_model_of_permission_boundary():
    plan = route_personal_request("Check my Gmail and reply to the recruiter")
    context = build_specialist_context(plan)
    assert "PermissionBroker" in context
    assert "Do not assume access" in context
