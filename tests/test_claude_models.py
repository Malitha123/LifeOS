"""Claude family-name resolution: the resolver, the Anthropic client
boundary, pricing for the family-resolved models, and the tag / tier maps
that name families."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import claude_models
from api.services.claude_models import LATEST_KNOWN, newest_in_family, resolve_claude_model

pytestmark = pytest.mark.unit

# The production fetcher, captured at import -- before the suite-wide
# conftest fixture replaces it for each test.
_REAL_FETCH = claude_models._fetch_model_ids


# A synthetic models list: dated snapshots, an older family member, a Fable
# model newer than everything else, and an undated alias that ties a dated
# snapshot of the same version.
FAKE_MODELS = [
    "claude-fable-9",
    "claude-mythos-9",
    "claude-opus-4-8",
    "claude-opus-6-1-20271201",
    "claude-sonnet-6-0-20270901",
    "claude-sonnet-6-0",
    "claude-sonnet-5-5",
    "claude-haiku-4-5-20251001",
    "claude-haiku-6-0-20271001",
    "claude-3-haiku-20240307",
]


class _FakeFetch:
    """Stands in for the models-list call; counts calls."""

    def __init__(self, result=FAKE_MODELS, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.result) if self.result is not None else None


@pytest.fixture
def fetch(monkeypatch):
    fake = _FakeFetch()
    monkeypatch.setattr(claude_models, "_fetch_model_ids", fake)
    return fake


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def test_family_resolves_to_newest_in_family(fetch):
    assert resolve_claude_model("opus") == "claude-opus-6-1-20271201"
    assert resolve_claude_model("haiku") == "claude-haiku-6-0-20271001"


def test_undated_alias_preferred_over_dated_snapshot_of_same_version(fetch):
    assert resolve_claude_model("sonnet") == "claude-sonnet-6-0"
    # Order in the list doesn't matter.
    assert newest_in_family(list(reversed(FAKE_MODELS)), "sonnet") == "claude-sonnet-6-0"


def test_fable_and_other_families_never_chosen(fetch):
    for family in ("haiku", "sonnet", "opus"):
        assert "fable" not in resolve_claude_model(family)
        assert "mythos" not in resolve_claude_model(family)
    assert resolve_claude_model("fable") == "fable"
    assert newest_in_family(FAKE_MODELS, "fable") is None


def test_full_id_passes_through_unchanged(fetch):
    for model_id in ("claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-fable-5", "local", ""):
        assert resolve_claude_model(model_id) == model_id
    assert fetch.calls == 0


def test_case_and_whitespace_ignored_for_family_names(fetch):
    assert resolve_claude_model("  Sonnet ") == "claude-sonnet-6-0"
    assert resolve_claude_model("OPUS") == "claude-opus-6-1-20271201"


def test_family_missing_from_list_falls_back_to_latest_known(monkeypatch):
    monkeypatch.setattr(claude_models, "_fetch_model_ids", _FakeFetch(["claude-sonnet-6-0"]))
    assert resolve_claude_model("haiku") == LATEST_KNOWN["haiku"]


def test_list_failure_falls_back_to_latest_known_without_raising(monkeypatch):
    monkeypatch.setattr(claude_models, "_fetch_model_ids", _FakeFetch(exc=RuntimeError("synthetic outage")))
    assert resolve_claude_model("haiku") == LATEST_KNOWN["haiku"]
    assert resolve_claude_model("sonnet") == LATEST_KNOWN["sonnet"]
    assert resolve_claude_model("opus") == LATEST_KNOWN["opus"]


def test_no_api_key_resolves_from_latest_known_without_a_request(monkeypatch):
    from config.settings import settings
    import anthropic

    monkeypatch.setattr(claude_models, "_fetch_model_ids", _REAL_FETCH)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: pytest.fail("no request without a key"))
    assert resolve_claude_model("opus") == LATEST_KNOWN["opus"]
    assert not claude_models.CACHE_PATH.exists()


def test_real_fetcher_uses_models_list_with_key(monkeypatch):
    from config.settings import settings
    import anthropic

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-synthetic")
    seen = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.models = SimpleNamespace(
                list=lambda **_: [SimpleNamespace(id=i) for i in FAKE_MODELS]
            )

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    monkeypatch.setattr(claude_models, "_fetch_model_ids", _REAL_FETCH)
    assert resolve_claude_model("sonnet") == "claude-sonnet-6-0"
    assert seen["api_key"] == "sk-synthetic"
    assert seen["timeout"] <= 10.0


def test_disk_cache_is_shared_and_respected_within_ttl(fetch):
    assert resolve_claude_model("opus") == "claude-opus-6-1-20271201"
    assert fetch.calls == 1
    cached = json.loads(claude_models.CACHE_PATH.read_text())
    assert cached["models"] == FAKE_MODELS
    # A fresh process (no in-memory memo) reads the disk cache, no fetch.
    claude_models.reset_cache()
    assert resolve_claude_model("sonnet") == "claude-sonnet-6-0"
    assert fetch.calls == 1


def test_cache_refreshed_after_ttl(fetch, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "anthropic_models_ttl_seconds", 60)
    resolve_claude_model("opus")
    assert fetch.calls == 1
    stale = {"checked_at": time.time() - 120, "models": ["claude-opus-4-8"]}
    claude_models.CACHE_PATH.write_text(json.dumps(stale))
    claude_models.reset_cache()
    fetch.result = FAKE_MODELS + ["claude-opus-7-0"]
    assert resolve_claude_model("opus") == "claude-opus-7-0"
    assert fetch.calls == 2


def test_failed_refresh_keeps_last_good_list_and_waits_a_ttl(fetch, monkeypatch, caplog):
    from config.settings import settings

    monkeypatch.setattr(settings, "anthropic_models_ttl_seconds", 60)
    stale = {"checked_at": time.time() - 120, "models": ["claude-opus-6-1", "claude-sonnet-6-0"]}
    claude_models.CACHE_PATH.write_text(json.dumps(stale))
    fetch.exc = RuntimeError("synthetic outage")
    with caplog.at_level("WARNING", logger="api.services.claude_models"):
        assert resolve_claude_model("opus") == "claude-opus-6-1"
        assert resolve_claude_model("sonnet") == "claude-sonnet-6-0"
        claude_models.reset_cache()  # another process: sees the recorded attempt on disk
        assert resolve_claude_model("opus") == "claude-opus-6-1"
    assert fetch.calls == 1
    assert sum("refresh failed" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# Anthropic client boundary
# ---------------------------------------------------------------------------

def _anthropic_response(model):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
        model=model,
        stop_reason="end_turn",
    )


def _client(model):
    from api.services.llm_client import AnthropicLLMClient

    return AnthropicLLMClient(api_key="sk-synthetic", model=model)


def test_client_create_sends_resolved_family(fetch):
    client = _client("sonnet")
    client._sync_client = MagicMock()
    client._sync_client.messages.create.return_value = _anthropic_response("claude-sonnet-6-0")
    client.create([{"role": "user", "content": "hi"}])
    assert client._sync_client.messages.create.call_args.kwargs["model"] == "claude-sonnet-6-0"
    assert client.model == "claude-sonnet-6-0"


def test_client_create_sends_pinned_id_unchanged(fetch):
    client = _client("claude-sonnet-4-6")
    client._sync_client = MagicMock()
    client._sync_client.messages.create.return_value = _anthropic_response("claude-sonnet-4-6")
    client.create([{"role": "user", "content": "hi"}])
    assert client._sync_client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_client_default_model_is_the_newest_haiku(fetch, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "anthropic_model", "haiku")
    assert _client(None).model == "claude-haiku-6-0-20271001"


async def test_client_acreate_sends_resolved_family(fetch):
    client = _client("opus")
    client._async_client = MagicMock()
    client._async_client.messages.create = AsyncMock(return_value=_anthropic_response("x"))
    await client.acreate([{"role": "user", "content": "hi"}])
    assert client._async_client.messages.create.call_args.kwargs["model"] == "claude-opus-6-1-20271201"


async def test_client_astream_sends_resolved_family(fetch):
    client = _client("haiku")
    seen = {}

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def get_final_message(self):
            return _anthropic_response("x")

    def _stream(**kwargs):
        seen.update(kwargs)
        return _Stream()

    client._async_client = MagicMock()
    client._async_client.messages.stream = _stream
    async for _ in client.astream([{"role": "user", "content": "hi"}]):
        pass
    assert seen["model"] == "claude-haiku-6-0-20271001"


def test_managed_executor_resolves_its_model(fetch):
    from api.services.agent_worker.managed_executor import ManagedExecutor

    executor = ManagedExecutor(
        session_store=MagicMock(), transcript_store=MagicMock(), driver=MagicMock(),
        agent_id="agent-synthetic", environment_id="env-synthetic", model="sonnet",
    )
    assert executor.model == "claude-sonnet-6-0"
    pinned = ManagedExecutor(
        session_store=MagicMock(), transcript_store=MagicMock(), driver=MagicMock(),
        agent_id="agent-synthetic", environment_id="env-synthetic", model="claude-sonnet-4-6",
    )
    assert pinned.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Defaults, tags and tiers name families
# ---------------------------------------------------------------------------

def test_settings_defaults_are_family_names():
    from config.settings import Settings

    fields = Settings.model_fields
    assert fields["anthropic_model"].default == "haiku"
    assert fields["anthropic_specialist_model"].default == "sonnet"
    assert fields["agent_preflight_model"].default == "haiku"
    assert fields["agent_managed_model"].default == "sonnet"
    assert fields["anthropic_models_ttl_seconds"].default == 86400


@pytest.mark.parametrize("tag,expected", [
    ("#cloud-haiku", "claude-haiku-6-0-20271001"),
    ("cloud-sonnet", "claude-sonnet-6-0"),
])
def test_cloud_tags_resolve_to_newest_family_model(fetch, tag, expected):
    from api.services.agent_worker.execution import parse_legacy_route_alias

    result = parse_legacy_route_alias(tag)
    assert result.request.executor == "claude"
    assert result.request.model_id == expected


@pytest.mark.parametrize("word,expected", [
    ("haiku", "claude-haiku-6-0-20271001"),
    ("sonnet", "claude-sonnet-6-0"),
    ("opus", "claude-opus-6-1-20271201"),
])
def test_escalation_tiers_resolve_to_newest_family_model(fetch, word, expected):
    from api.services.agent_loop import resolve_model_alias, resolve_orchestrator_model

    assert resolve_model_alias(word) == expected
    model, escalated = resolve_orchestrator_model(
        [], f"use {word}", base_model="claude-haiku-4-5", escalation_model="",
    )
    assert (model, escalated) == (expected, True)


def test_preflight_cloud_tags_name_families():
    from api.services.agent_worker import preflight

    assert preflight.MODEL_HAIKU == "haiku"
    assert preflight.MODEL_SONNET == "sonnet"


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

M = 1_000_000


@pytest.mark.parametrize("model,input_rate,output_rate", [
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-sonnet-5-5", 2.0, 10.0),
    ("claude-opus-5-5", 4.0, 20.0),
])
def test_55_model_rates(model, input_rate, output_rate):
    from api.services.agent_worker.pricing import cost_for

    assert cost_for(model, M, 0) == pytest.approx(input_rate)
    assert cost_for(model, 0, M) == pytest.approx(output_rate)


def test_haiku_55_long_prompt_tier_applies_per_request():
    from api.services.agent_worker.pricing import cost_for

    # At or under 100K input tokens: standard rate.
    assert cost_for("claude-haiku-5-5", 100_000, M, single_request=True) == pytest.approx(
        100_000 * 0.10 / M + 0.50
    )
    # Over 100K (cache reads count toward the threshold): the whole request
    # is billed at $0.50/$2.50.
    assert cost_for("claude-haiku-5-5", 60_000, M, cache_read_tokens=50_000, single_request=True) == pytest.approx(
        60_000 * 0.50 / M + 2.50 + 50_000 * 0.50 * 0.10 / M
    )
    # Totals across several requests aren't attributed to a tier.
    assert cost_for("claude-haiku-5-5", 500_000, 0) == pytest.approx(0.05)


def test_55_cache_read_multipliers():
    from api.services.agent_worker.pricing import cost_for

    assert cost_for("claude-opus-5-5", 0, 0, cache_read_tokens=M) == pytest.approx(4.0 * 0.05)
    assert cost_for("claude-sonnet-5-5", 0, 0, cache_read_tokens=M) == pytest.approx(2.0 * 0.05)
    assert cost_for("claude-haiku-5-5", 0, 0, cache_read_tokens=M) == pytest.approx(0.10 * 0.10)
    assert cost_for("claude-sonnet-5", 0, 0, cache_read_tokens=M) == pytest.approx(2.0 * 0.10)


@pytest.mark.parametrize("model,expected_input", [
    ("claude-opus-6-1-20271201", 4.0),   # unlisted, dated
    ("claude-sonnet-6-0", 2.0),          # unlisted
    ("claude-haiku-6-0", 0.10),
    ("sonnet", 2.0),                     # bare family name
])
def test_unlisted_family_member_priced_at_family_newest_rate(model, expected_input):
    from api.services.agent_worker.pricing import cost_for, is_known_model

    assert is_known_model(model)
    assert cost_for(model, M, 0) == pytest.approx(expected_input)


def test_unlisted_non_family_model_stays_unknown():
    from api.services.agent_worker.pricing import fallback_rates, is_known_model, rates_for

    assert not is_known_model("claude-fable-9")
    assert rates_for("claude-fable-9") == fallback_rates()


class _FakeRoundClient:
    """One agent-loop round on `model` with the given usage."""

    def __init__(self, model, input_tokens, output_tokens):
        self.model = model
        self._usage = (input_tokens, output_tokens)

    async def astream(self, messages, *, system=None, max_tokens=4096,
                      tools=None, temperature=None, timeout=None):
        from api.services.llm_client import LLMUsage
        yield {"type": "text", "content": "42."}
        yield {
            "type": "done",
            "usage": LLMUsage(input_tokens=self._usage[0], output_tokens=self._usage[1]),
            "finish_reason": "end_turn",
        }


async def _agent_loop_result(client):
    from unittest.mock import patch
    from api.services import agent_loop

    with patch.object(agent_loop, "_select_client", return_value=client):
        events = [e async for e in agent_loop.run_agent_loop("what is six times seven")]
    return next(e["result"] for e in events if e["type"] == "result")


async def test_agent_loop_prices_a_long_haiku_55_request_at_the_long_tier():
    result = await _agent_loop_result(_FakeRoundClient("claude-haiku-5-5", 150_000, 1_000))
    assert result.model == "claude-haiku-5-5"
    assert result.unpriced is False
    assert result.total_cost_usd == pytest.approx(150_000 * 0.50 / M + 1_000 * 2.50 / M)


async def test_agent_loop_prices_unlisted_family_model_not_unpriced():
    result = await _agent_loop_result(_FakeRoundClient("claude-sonnet-6-0", M, 0))
    assert result.unpriced is False
    assert result.total_cost_usd == pytest.approx(2.0)
