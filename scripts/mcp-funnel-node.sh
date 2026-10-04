#!/usr/bin/env bash
# The public MCP node: a second, unprivileged tailscaled (userspace networking)
# with its own tailnet hostname, whose port 443 is published with Funnel to the
# MCP HTTP transport. Connector platforms only connect out on port 443, and
# Funnel exposure is port-wide, so the public paths get a node of their own and
# LifeOS's own 443 stays tailnet-only.
#
# Usage: mcp-funnel-node.sh apply|check
#   apply  wait for the node to be logged in, then publish the public paths
#   check  exit 0 only when every public path is published on 443 with Funnel
# Exit 3 means the node needs a login (run `tailscale --socket=<sock> up`).
set -uo pipefail

SOCKET="${LIFEOS_MCP_FUNNEL_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/lifeos-mcp-ts.sock}"
MCP_TARGET="http://127.0.0.1:${LIFEOS_MCP_HTTP_PORT:-8765}"
WAIT_SECONDS="${LIFEOS_MCP_FUNNEL_WAIT_SECONDS:-60}"
PUBLIC_PATHS=(
    /mcp
    /.well-known/oauth-protected-resource
    /.well-known/oauth-authorization-server
    /oauth/register
    /oauth/token
    /oauth/revoke
)

ts() { tailscale --socket="$SOCKET" "$@"; }

backend_state() {
    ts status --json 2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("BackendState") or "")
except ValueError:
    print("")
'
}

# Succeeds when every public path proxies to the MCP transport on 443 and 443
# is Funnel-exposed.
published() {
    ts serve status --json 2>/dev/null | python3 -c '
import json, sys
target, paths = sys.argv[1], sys.argv[2:]
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
if not any(on and hp.rsplit(":", 1)[-1] == "443" for hp, on in (data.get("AllowFunnel") or {}).items()):
    sys.exit(1)
handlers = {}
for hp, web in (data.get("Web") or {}).items():
    if hp.rsplit(":", 1)[-1] == "443":
        handlers.update(web.get("Handlers") or {})
for path in paths:
    proxy = ((handlers.get(path) or {}).get("Proxy") or "").rstrip("/")
    if proxy != target + path:
        sys.exit(1)
' "$MCP_TARGET" "${PUBLIC_PATHS[@]}"
}

apply() {
    local state="" waited=0 path
    while (( waited <= WAIT_SECONDS )); do
        state=$(backend_state)
        [[ "$state" == "Running" || "$state" == "NeedsLogin" ]] && break
        sleep 2
        waited=$((waited + 2))
    done
    if [[ "$state" == "NeedsLogin" ]]; then
        echo "MCP Funnel node is logged out; run: tailscale --socket=$SOCKET up --hostname=<name>" >&2
        return 3
    fi
    if [[ "$state" != "Running" ]]; then
        echo "MCP Funnel node did not reach Running (state: ${state:-unreachable})" >&2
        return 1
    fi
    for path in "${PUBLIC_PATHS[@]}"; do
        ts funnel --bg --https=443 --set-path "$path" "$MCP_TARGET$path" > /dev/null 2>&1
    done
    if ! published; then
        echo "MCP Funnel node: public paths are not all published after apply" >&2
        return 1
    fi
}

case "${1:-}" in
    apply) apply ;;
    check)
        [[ "$(backend_state)" == "NeedsLogin" ]] && exit 3
        published
        ;;
    *) echo "usage: $0 apply|check" >&2; exit 2 ;;
esac
