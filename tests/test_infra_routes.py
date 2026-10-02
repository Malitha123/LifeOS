"""Declared tailnet routes (setup-tailscale.sh) and the infra watchdog.

The scripts run under bash with fake `tailscale`, `curl`, `pgrep` and
`systemd-run` binaries first on PATH. Each fake appends its argv to
$FAKE_ACTIONS so tests assert exactly what was invoked. The fake `tailscale`
keeps a JSON route table so `serve status --json` reflects what was applied.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP = REPO_ROOT / "scripts" / "setup-tailscale.sh"
WATCHDOG = REPO_ROOT / "scripts" / "infra-watchdog.sh"

HOST = "example-host.tailnet.ts.net"

TAILSCALE_FAKE = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_ACTIONS"], "a") as f:
    f.write("tailscale " + " ".join(args) + "\\n")
state_path = os.environ["FAKE_TS_STATE"]
try:
    state = json.load(open(state_path))
except OSError:
    state = {}
if args[:2] == ["serve", "reset"]:
    state = {}
elif args[:2] == ["serve", "status"] and "--json" in args:
    web = {}
    for key, target in state.items():
        port, path = key.split("|", 1)
        web.setdefault(os.environ["FAKE_TS_HOST"] + ":" + port, {"Handlers": {}})["Handlers"][path] = {"Proxy": target}
    print(json.dumps({"Web": web}))
    sys.exit(0)
elif args[:2] == ["serve", "status"]:
    print("fake serve status")
    sys.exit(0)
elif args[0] in ("serve", "funnel"):
    port = next(a.split("=", 1)[1] for a in args if a.startswith("--https="))
    path = args[args.index("--set-path") + 1]
    target = args[-1]
    if port != os.environ.get("FAKE_TS_DROP_PORT"):
        state[port + "|" + path] = target
json.dump(state, open(state_path, "w"))
'''

CURL_FAKE = '''#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_ACTIONS"
case "$*" in
  *api.telegram.org*) exit "${FAKE_TG_RC:-0}" ;;
  *) exit "${FAKE_HEALTH_RC:-0}" ;;
esac
'''

PGREP_FAKE = '''#!/usr/bin/env bash
echo "pgrep $*" >> "$FAKE_ACTIONS"
[ "${FAKE_OBSIDIAN:-0}" = "1" ] && exit 0 || exit 1
'''

SYSTEMD_RUN_FAKE = '''#!/usr/bin/env bash
echo "systemd-run $*" >> "$FAKE_ACTIONS"
exit 0
'''


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("tailscale", TAILSCALE_FAKE),
        ("curl", CURL_FAKE),
        ("pgrep", PGREP_FAKE),
        ("systemd-run", SYSTEMD_RUN_FAKE),
    ):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=xtoken\nTELEGRAM_CHAT_ID=123\n")
    routes = tmp_path / "routes.local"
    actions = tmp_path / "actions.log"
    state_dir = tmp_path / "state"
    ts_state = tmp_path / "ts-state.json"

    class Box:
        pass

    b = Box()
    b.routes, b.actions, b.state_dir, b.ts_state = routes, actions, state_dir, ts_state

    def run(script, extra=None):
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_ACTIONS": str(actions),
            "FAKE_TS_STATE": str(ts_state),
            "FAKE_TS_HOST": HOST,
            "LIFEOS_TAILSCALE_ROUTES_FILE": str(routes),
            "LIFEOS_INFRA_STATE_DIR": str(state_dir),
            "ENV_FILE": str(env_file),
            "LIFEOS_WATCHDOG_CURL": str(bin_dir / "curl"),
            "HOME": str(tmp_path),
        }
        env.update(extra or {})
        return subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30
        )

    b.setup = lambda extra=None: run(SETUP, extra)
    b.watch = lambda extra=None: run(WATCHDOG, extra)
    b.log = lambda: actions.read_text().splitlines() if actions.exists() else []
    b.telegrams = lambda: [a for a in b.log() if "api.telegram.org" in a]
    b.table = lambda: json.loads(ts_state.read_text()) if ts_state.exists() else {}
    return b


PEBBLE_LINE = "https=8443 path=/webhooks/pebble target=http://127.0.0.1:9790/webhooks/pebble\n"


# ---- setup-tailscale.sh -----------------------------------------------------


def test_no_routes_file_applies_only_the_lifeos_route(box):
    r = box.setup()
    assert r.returncode == 0, r.stderr
    assert box.table() == {"443|/": "http://127.0.0.1:8000"}


def test_lifeos_port_is_honoured(box):
    box.setup({"LIFEOS_PORT": "8123"})
    assert box.table() == {"443|/": "http://127.0.0.1:8123"}


def test_declared_route_is_applied_and_visible_in_status(box):
    box.routes.write_text("# pebble ring\n" + PEBBLE_LINE)
    r = box.setup()
    assert r.returncode == 0, r.stderr
    assert box.table()["8443|/webhooks/pebble"] == "http://127.0.0.1:9790/webhooks/pebble"
    assert (
        "tailscale serve --bg --https=8443 --set-path /webhooks/pebble "
        "http://127.0.0.1:9790/webhooks/pebble"
    ) in box.log()


def test_never_resets_and_leaves_undeclared_routes_alone(box):
    box.ts_state.write_text(json.dumps({"9443|/other": "http://127.0.0.1:1"}))
    box.routes.write_text(PEBBLE_LINE)
    assert box.setup().returncode == 0
    assert box.setup().returncode == 0  # idempotent
    assert not any("reset" in a for a in box.log())
    table = box.table()
    assert table["9443|/other"] == "http://127.0.0.1:1"
    assert len(table) == 3


def test_missing_route_after_apply_fails_and_names_it(box):
    box.routes.write_text(PEBBLE_LINE)
    r = box.setup({"FAKE_TS_DROP_PORT": "8443"})
    assert r.returncode != 0
    assert "MISSING route: https=8443 path=/webhooks/pebble" in r.stderr


def test_funnel_rejected_without_opt_in(box):
    box.routes.write_text(PEBBLE_LINE.rstrip("\n") + " funnel=on\n")
    r = box.setup()
    assert r.returncode != 0
    assert "LIFEOS_TAILSCALE_ALLOW_FUNNEL" in r.stderr
    assert not any(a.startswith("tailscale funnel") or "serve --bg" in a for a in box.log())


def test_funnel_allowed_with_opt_in(box):
    box.routes.write_text(PEBBLE_LINE.rstrip("\n") + " funnel=on\n")
    r = box.setup({"LIFEOS_TAILSCALE_ALLOW_FUNNEL": "true"})
    assert r.returncode == 0, r.stderr
    assert any(a.startswith("tailscale funnel --bg --https=8443") for a in box.log())


def test_invalid_route_line_applies_nothing(box):
    box.routes.write_text("https=abc path=nope target=ftp://x\n")
    r = box.setup()
    assert r.returncode != 0
    assert box.log() == []


# ---- infra-watchdog.sh ------------------------------------------------------


def test_watchdog_all_routes_present_does_nothing(box):
    box.routes.write_text(PEBBLE_LINE)
    box.setup()
    box.actions.write_text("")
    r = box.watch()
    assert r.returncode == 0
    assert not any("serve --bg" in a for a in box.log())
    assert box.telegrams() == []
    assert "all present" in (box.state_dir / "infra-watchdog.log").read_text()


def test_watchdog_reapplies_a_missing_route_without_alerting(box):
    box.routes.write_text(PEBBLE_LINE)
    box.setup()
    box.ts_state.write_text(json.dumps({"443|/": "http://127.0.0.1:8000"}))  # 8443 vanished
    r = box.watch()
    assert r.returncode == 0
    assert "8443|/webhooks/pebble" in box.table()
    assert box.telegrams() == []
    assert not any("reset" in a for a in box.log())
    assert "re-applied" in (box.state_dir / "infra-watchdog.log").read_text()


def test_watchdog_alerts_once_per_cooldown_when_route_cannot_be_restored(box):
    box.routes.write_text(PEBBLE_LINE)
    extra = {"FAKE_TS_DROP_PORT": "8443"}
    assert box.watch(extra).returncode == 0
    assert box.watch(extra).returncode == 0
    assert len(box.telegrams()) == 1
    assert "suppressed (cooldown)" in (box.state_dir / "infra-watchdog.log").read_text()


def test_watchdog_alerts_again_after_cooldown_expires(box):
    box.routes.write_text(PEBBLE_LINE)
    extra = {"FAKE_TS_DROP_PORT": "8443"}
    box.watch(extra)
    stamp = box.state_dir / "infra-watchdog-alert-routes.stamp"
    stamp.write_text("1")  # long ago
    box.watch(extra)
    assert len(box.telegrams()) == 2


def test_failed_telegram_send_does_not_start_a_cooldown(box):
    box.routes.write_text(PEBBLE_LINE)
    extra = {"FAKE_TS_DROP_PORT": "8443", "FAKE_TG_RC": "1"}
    box.watch(extra)
    box.watch(extra)
    assert len(box.telegrams()) == 2


def test_watchdog_pebble_unset_skips_the_check(box):
    box.watch({"FAKE_HEALTH_RC": "1"})
    assert not any("curl" in a and "api.telegram.org" not in a for a in box.log())


def test_watchdog_pebble_alerts_on_third_consecutive_failure(box):
    extra = {"LIFEOS_PEBBLE_HEALTH_URL": "http://127.0.0.1:9790/health", "FAKE_HEALTH_RC": "22"}
    box.watch(extra)
    box.watch(extra)
    assert box.telegrams() == []
    box.watch(extra)
    assert len(box.telegrams()) == 1


def test_watchdog_pebble_success_resets_the_strike_count(box):
    url = {"LIFEOS_PEBBLE_HEALTH_URL": "http://127.0.0.1:9790/health"}
    box.watch({**url, "FAKE_HEALTH_RC": "22"})
    box.watch({**url, "FAKE_HEALTH_RC": "22"})
    box.watch({**url, "FAKE_HEALTH_RC": "0"})
    box.watch({**url, "FAKE_HEALTH_RC": "22"})
    box.watch({**url, "FAKE_HEALTH_RC": "22"})
    assert box.telegrams() == []


def test_watchdog_ignores_obsidian_unless_expected(box):
    box.watch()
    assert not any(a.startswith(("pgrep", "systemd-run")) for a in box.log())


def test_watchdog_obsidian_running_does_not_relaunch(box):
    box.watch({"LIFEOS_EXPECT_OBSIDIAN_SYNC": "true", "FAKE_OBSIDIAN": "1"})
    assert not any(a.startswith("systemd-run") for a in box.log())
    assert box.telegrams() == []


def test_watchdog_obsidian_relaunches_then_alerts_if_still_absent(box):
    extra = {"LIFEOS_EXPECT_OBSIDIAN_SYNC": "true", "FAKE_OBSIDIAN": "0"}
    box.watch(extra)
    launches = [a for a in box.log() if a.startswith("systemd-run --user --collect --unit=obsidian-session-")]
    assert len(launches) == 1
    assert launches[0].endswith("snap run obsidian")
    assert box.telegrams() == []
    box.watch(extra)
    assert len(box.telegrams()) == 1
    assert len([a for a in box.log() if a.startswith("systemd-run")]) == 1


def test_watchdog_obsidian_recovery_clears_the_relaunch_marker(box):
    extra = {"LIFEOS_EXPECT_OBSIDIAN_SYNC": "true"}
    box.watch({**extra, "FAKE_OBSIDIAN": "0"})
    box.watch({**extra, "FAKE_OBSIDIAN": "1"})
    box.watch({**extra, "FAKE_OBSIDIAN": "0"})
    assert box.telegrams() == []
    assert len([a for a in box.log() if a.startswith("systemd-run")]) == 2


def test_watchdog_obsidian_launch_command_is_configurable(box):
    box.watch(
        {
            "LIFEOS_EXPECT_OBSIDIAN_SYNC": "true",
            "LIFEOS_OBSIDIAN_LAUNCH_CMD": "systemd-run --user custom-launch",
        }
    )
    assert "systemd-run --user custom-launch" in box.log()


def test_watchdog_exits_zero_on_invalid_routes_file(box):
    box.routes.write_text("garbage\n")
    r = box.watch()
    assert r.returncode == 0
    assert len(box.telegrams()) == 1
