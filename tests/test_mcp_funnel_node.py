"""The public MCP node script and its infra-watchdog check.

Both run under bash with a fake `tailscale` first on PATH. The fake keeps one
serve table per `--socket` (the main node has none), so tests can tell the
public MCP node's routes apart from the host's own.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_infra_routes import CURL_FAKE, PGREP_FAKE

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = REPO_ROOT / "scripts" / "mcp-funnel-node.sh"
WATCHDOG = REPO_ROOT / "scripts" / "infra-watchdog.sh"
UNIT = REPO_ROOT / "config" / "systemd" / "user" / "lifeos-mcp-funnel.service"

PUBLIC_PATHS = [
    "/mcp",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/oauth/register",
    "/oauth/token",
    "/oauth/revoke",
]

TAILSCALE_FAKE = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
node = "main"
if args and args[0].startswith("--socket="):
    node = "mcp"
    args = args[1:]
with open(os.environ["FAKE_ACTIONS"], "a") as f:
    f.write(node + " tailscale " + " ".join(args) + "\\n")
path = os.path.join(os.environ["FAKE_TS_DIR"], node + ".json")
try:
    state = json.load(open(path))
except OSError:
    state = {}
host = "lifeos-mcp.example.ts.net" if node == "mcp" else "example-host.example.ts.net"
if args[:2] == ["status", "--json"]:
    backend = os.environ.get("FAKE_MCP_BACKEND", "Running") if node == "mcp" else "Running"
    print(json.dumps({"BackendState": backend}))
    sys.exit(0)
if args[:2] == ["serve", "status"]:
    web, funnel = {}, {}
    for key, target in state.items():
        port, p = key.split("|", 1)
        if port == "funnel":
            funnel[host + ":" + p] = True
            continue
        web.setdefault(host + ":" + port, {"Handlers": {}})["Handlers"][p] = {"Proxy": target}
    print(json.dumps({"Web": web, "AllowFunnel": funnel}))
    sys.exit(0)
if args[0] in ("serve", "funnel") and "--set-path" in args:
    if node == "mcp" and os.environ.get("FAKE_APPLY_BROKEN"):
        sys.exit(1)
    port = next(a.split("=", 1)[1] for a in args if a.startswith("--https="))
    state[port + "|" + args[args.index("--set-path") + 1]] = args[-1]
    if args[0] == "funnel":
        state["funnel|" + port] = True
json.dump(state, open(path, "w"))
'''

SYSTEMCTL_FAKE = '''#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_ACTIONS"
case "$*" in
  *is-active*) [ "${FAKE_UNIT_ACTIVE:-1}" = "1" ] && exit 0 || exit 3 ;;
esac
exit 0
'''


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("tailscale", TAILSCALE_FAKE),
        ("systemctl", SYSTEMCTL_FAKE),
        ("curl", CURL_FAKE),
        ("pgrep", PGREP_FAKE),
    ):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    ts_dir = tmp_path / "ts"
    ts_dir.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=xtoken\nTELEGRAM_CHAT_ID=123\n")
    actions = tmp_path / "actions.log"

    class Env:
        pass

    e = Env()
    e.base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_ACTIONS": str(actions),
        "FAKE_TS_DIR": str(ts_dir),
        "LIFEOS_MCP_FUNNEL_SOCKET": str(tmp_path / "mcp.sock"),
        "LIFEOS_MCP_FUNNEL_WAIT_SECONDS": "0",
        "LIFEOS_TAILSCALE_ROUTES_FILE": str(tmp_path / "routes.local"),
        "LIFEOS_INFRA_STATE_DIR": str(tmp_path / "state"),
        "ENV_FILE": str(env_file),
        "LIFEOS_WATCHDOG_CURL": str(bin_dir / "curl"),
        "HOME": str(tmp_path),
    }

    def run(script, *args, extra=None):
        return subprocess.run(
            ["bash", str(script), *args], env={**e.base, **(extra or {})},
            capture_output=True, text=True, timeout=30,
        )

    e.node = lambda *a, extra=None: run(NODE, *a, extra=extra)
    e.watch = lambda extra=None: run(WATCHDOG, extra={"LIFEOS_MCP_FUNNEL_NODE": "true", **(extra or {})})
    e.table = lambda node="mcp": json.loads((ts_dir / f"{node}.json").read_text()) if (ts_dir / f"{node}.json").exists() else {}
    e.log = lambda: actions.read_text().splitlines() if actions.exists() else []
    e.telegrams = lambda: [a for a in e.log() if "api.telegram.org" in a]
    e.watchdog_log = lambda: (tmp_path / "state" / "infra-watchdog.log").read_text()
    return e


def _published(table: dict) -> bool:
    return table.get("funnel|443") is True and all(
        table.get(f"443|{p}") == f"http://127.0.0.1:8765{p}" for p in PUBLIC_PATHS
    )


def test_apply_publishes_every_public_path_on_443_of_the_mcp_node_only(env):
    r = env.node("apply")
    assert r.returncode == 0, r.stderr
    assert _published(env.table())
    assert set(env.table()) == {"funnel|443", *(f"443|{p}" for p in PUBLIC_PATHS)}
    assert env.table("main") == {}
    assert env.node("check").returncode == 0


def test_apply_honours_the_mcp_http_port(env):
    assert env.node("apply", extra={"LIFEOS_MCP_HTTP_PORT": "9123"}).returncode == 0
    assert env.table()["443|/mcp"] == "http://127.0.0.1:9123/mcp"


def test_check_fails_when_a_path_is_missing_or_not_public(env):
    env.node("apply")
    table = env.table()
    table.pop("443|/oauth/token")
    (Path(env.base["FAKE_TS_DIR"]) / "mcp.json").write_text(json.dumps(table))
    assert env.node("check").returncode == 1
    env.node("apply")
    table = env.table()
    table.pop("funnel|443")
    (Path(env.base["FAKE_TS_DIR"]) / "mcp.json").write_text(json.dumps(table))
    assert env.node("check").returncode == 1


def test_logged_out_node_reports_3_and_publishes_nothing(env):
    extra = {"FAKE_MCP_BACKEND": "NeedsLogin"}
    r = env.node("apply", extra=extra)
    assert r.returncode == 3
    assert "logged out" in r.stderr
    assert env.table() == {}
    assert env.node("check", extra=extra).returncode == 3


def test_apply_that_does_not_take_effect_fails(env):
    r = env.node("apply", extra={"FAKE_APPLY_BROKEN": "1"})
    assert r.returncode == 1
    assert "not all published" in r.stderr


def test_watchdog_republishes_missing_paths_without_alerting(env):
    env.watch()
    assert _published(env.table())
    assert "mcp-funnel: public paths were missing; re-applied" in env.watchdog_log()
    assert env.telegrams() == []


def test_watchdog_leaves_a_published_node_alone(env):
    env.node("apply")
    before = len(env.log())
    env.watch()
    assert not any(a.startswith("mcp tailscale funnel") for a in env.log()[before:])
    assert "mcp-funnel: published" in env.watchdog_log()


def test_watchdog_alerts_when_the_node_is_logged_out(env):
    env.watch({"FAKE_MCP_BACKEND": "NeedsLogin"})
    assert len(env.telegrams()) == 1
    assert "mcp-funnel: node logged out" in env.watchdog_log()


def test_watchdog_alerts_when_paths_cannot_be_restored(env):
    env.watch({"FAKE_APPLY_BROKEN": "1"})
    assert len(env.telegrams()) == 1
    assert "could not be restored" in env.watchdog_log()


def test_watchdog_starts_an_inactive_unit(env):
    env.watch({"FAKE_UNIT_ACTIVE": "0"})
    assert "systemctl --user start lifeos-mcp-funnel.service" in env.log()


def test_watchdog_skips_the_node_unless_opted_in(env):
    env.watch({"LIFEOS_MCP_FUNNEL_NODE": "false"})
    assert not any(a.startswith("mcp tailscale") for a in env.log())
    assert not any("lifeos-mcp-funnel" in a for a in env.log())


def test_unit_runs_an_unprivileged_userspace_node_on_the_scripts_socket():
    text = UNIT.read_text()
    assert "--tun=userspace-networking" in text
    assert "--socket=%t/lifeos-mcp-ts.sock" in text
    assert "ExecStartPost=-__LIFEOS_DIR__/scripts/mcp-funnel-node.sh apply" in text
    assert "lifeos-mcp-ts.sock" in NODE.read_text()
