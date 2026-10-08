"""The remote-model probe: collecting model ids and classifying probe results.

Synthetic model ids and an in-process httpx transport; nothing leaves the box.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_remote_models.py"
LIVE = "accounts/example/models/live-model"
GONE = "accounts/example/models/retired-model"
FLAKY = "accounts/example/models/flaky-model"
REAL_CLIENT = httpx.Client
LOCKED = "accounts/example/models/locked-model"


@pytest.fixture
def probe_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location("check_remote_models", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.settings, "remote_llm_base_url", "https://provider.example.com/inference/v1")
    monkeypatch.setattr(mod.settings, "remote_llm_api_key", "synthetic-key")
    monkeypatch.setattr(mod.settings, "remote_llm_model", LIVE)
    monkeypatch.setattr(mod.settings, "remote_llm_model_options", f"{GONE}, {LIVE}")
    monkeypatch.setattr(mod.settings, "model_probe_sources", "")
    return mod


def _transport(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append((str(request.url), model, request.headers.get("authorization")))
        if model == GONE:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if model == FLAKY:
            return httpx.Response(503)
        if model == LOCKED:
            return httpx.Response(401)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    return httpx.MockTransport(handler)


def _run(probe_mod, monkeypatch, tmp_path, capsys, calls):
    def client(**kw):
        assert kw["timeout"] == probe_mod.TIMEOUT_SECONDS
        return REAL_CLIENT(transport=_transport(calls), **kw)

    monkeypatch.setattr(probe_mod.httpx, "Client", client)
    monkeypatch.setenv("LIFEOS_INFRA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("sys.argv", ["check_remote_models.py"])
    assert probe_mod.main() == 0
    return [line.split("\t", 1) for line in capsys.readouterr().out.splitlines()]


def test_collect_dedupes_settings_and_scans_sources(probe_mod, tmp_path):
    hermes = tmp_path / "config.yaml"
    hermes.write_text(f"model:\n  default: {FLAKY}\nproviders:\n  - model: {LIVE}\n    base_url: https://provider.example.com\n")
    where, errors = probe_mod.collect([str(hermes), str(tmp_path / "missing.yaml")])
    assert where[LIVE] == ["LifeOS settings", "LifeOS settings", str(hermes)]
    assert where[GONE] == ["LifeOS settings"]
    assert where[FLAKY] == [str(hermes)]
    assert list(errors) == [str(tmp_path / "missing.yaml")]


def test_retired_model_is_reported_at_once_with_where_it_is_configured(probe_mod, monkeypatch, tmp_path, capsys):
    calls: list = []
    problems = _run(probe_mod, monkeypatch, tmp_path, capsys, calls)
    assert [key for key, _ in problems] == [f"model:{GONE}"]
    assert "no longer served" in problems[0][1] and "LifeOS settings" in problems[0][1]
    urls = {url for url, _, _ in calls}
    assert urls == {"https://provider.example.com/inference/v1/chat/completions"}
    assert {auth for _, _, auth in calls} == {"Bearer synthetic-key"}
    assert sorted(model for _, model, _ in calls) == [LIVE, GONE]


def test_rejected_key_is_reported_at_once(probe_mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model_options", LOCKED)
    problems = _run(probe_mod, monkeypatch, tmp_path, capsys, [])
    assert [key for key, _ in problems] == [f"model:{LOCKED}"]
    assert "rejected the key" in problems[0][1]


def test_transient_failures_alert_only_after_three_runs_and_reset_on_success(probe_mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model_options", FLAKY)
    assert _run(probe_mod, monkeypatch, tmp_path, capsys, []) == []
    assert _run(probe_mod, monkeypatch, tmp_path, capsys, []) == []
    third = _run(probe_mod, monkeypatch, tmp_path, capsys, [])
    assert [key for key, _ in third] == [f"model:{FLAKY}"]
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model_options", "")
    assert _run(probe_mod, monkeypatch, tmp_path, capsys, []) == []
    state = json.loads((tmp_path / "state" / "model-probe.json").read_text())
    assert state["strikes"] == {}


def test_unreadable_source_alerts_after_three_runs(probe_mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model_options", "")
    monkeypatch.setattr(probe_mod.settings, "model_probe_sources", str(tmp_path / "gone.yaml"))
    for _ in range(2):
        assert _run(probe_mod, monkeypatch, tmp_path, capsys, []) == []
    third = _run(probe_mod, monkeypatch, tmp_path, capsys, [])
    assert third[0][0] == f"source:{tmp_path / 'gone.yaml'}"
    assert "not being checked" in third[0][1]


def test_unconfigured_provider_exits_2(probe_mod, monkeypatch):
    monkeypatch.setattr(probe_mod.settings, "remote_llm_api_key", "")
    monkeypatch.setattr("sys.argv", ["check_remote_models.py"])
    assert probe_mod.main() == 2


def test_ssh_source_reads_over_ssh(probe_mod, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        assert kw["timeout"] == probe_mod.SSH_TIMEOUT_SECONDS
        return probe_mod.subprocess.CompletedProcess(cmd, 0, stdout=f"model: {GONE}\n", stderr="")

    monkeypatch.setattr(probe_mod.subprocess, "run", fake_run)
    assert probe_mod.read_source("other-host:~/.hermes/config.yaml") == f"model: {GONE}\n"
    assert seen["cmd"][0] == "ssh" and "BatchMode=yes" in seen["cmd"]
    assert seen["cmd"][-2:] == ["other-host", "cat ~/.hermes/config.yaml"]


def test_a_recovered_model_clears_its_strikes(probe_mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model_options", FLAKY)
    for _ in range(2):
        assert _run(probe_mod, monkeypatch, tmp_path, capsys, []) == []
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model", FLAKY)
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model_options", "")
    real_probe = probe_mod.probe
    monkeypatch.setattr(probe_mod, "probe", lambda client, base, model: ("ok", ""))
    assert _run(probe_mod, monkeypatch, tmp_path, capsys, []) == []
    monkeypatch.setattr(probe_mod, "probe", real_probe)
    for _ in range(2):
        assert _run(probe_mod, monkeypatch, tmp_path, capsys, []) == []
    assert [k for k, _ in _run(probe_mod, monkeypatch, tmp_path, capsys, [])] == [f"model:{FLAKY}"]


def test_commented_out_model_ids_are_not_probed(probe_mod, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"# LIFEOS_REMOTE_LLM_MODEL={GONE}\nLIFEOS_REMOTE_LLM_MODEL={FLAKY}\n")
    hermes = tmp_path / "config.yaml"
    hermes.write_text(
        f"model:\n  # default: {GONE}\n  default: {LOCKED}\n"
        f'provider: {{label: "primary # production", model: {LIVE}}}\n'
    )
    where, _ = probe_mod.collect([str(env), str(hermes)])
    assert where[FLAKY] == [str(env)] and where[LOCKED] == [str(hermes)]
    assert str(hermes) in where[LIVE]
    assert where[GONE] == ["LifeOS settings"]


def test_models_and_sources_are_checked_in_parallel(probe_mod, monkeypatch, tmp_path, capsys):
    import time

    models = [f"accounts/example/models/slow-{i}" for i in range(6)]
    monkeypatch.setattr(probe_mod.settings, "remote_llm_model_options", ",".join(models))
    sources = [str(tmp_path / f"src-{i}") for i in range(4)]

    def slow_probe(client, base, model):
        time.sleep(0.5)
        return "ok", ""

    def slow_read(source):
        time.sleep(0.5)
        return ""

    monkeypatch.setattr(probe_mod, "probe", slow_probe)
    monkeypatch.setattr(probe_mod, "read_source", slow_read)
    monkeypatch.setattr(probe_mod.settings, "model_probe_sources", ",".join(sources))
    started = time.monotonic()
    _run(probe_mod, monkeypatch, tmp_path, capsys, [])
    assert time.monotonic() - started < 2.5  # serial would take 5.0+ seconds
    assert probe_mod.TIMEOUT_SECONDS <= 20 and probe_mod.SSH_TIMEOUT_SECONDS <= 20
