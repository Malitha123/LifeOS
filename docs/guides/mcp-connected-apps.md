# Connected Apps (Claude, ChatGPT)

**Status:** Complete
**Last Updated:** 2026-10-03
**Audience:** Operator

The Claude and ChatGPT apps can use LifeOS as a custom connector. They reach the MCP HTTP transport over a public Tailscale Funnel port. Each app is approved once, on a consent page that only the operator's own tailnet devices can open. A connected app gets the restricted "read + safe writes" tool tier, never the full catalog.

What the tier allows, the token rules and the residual risks are in [MCP OAuth](../specs/technical/mcp-oauth.md).

---

## How the pieces fit

| Port | Exposure | Paths | Serves |
|---|---|---|---|
| `443` | tailnet only | `/` | LifeOS itself; never public |
| `8443` | tailnet only | `/oauth/authorize` | The consent page |
| `10000` | public (Funnel) | `/mcp`, `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `/oauth/register`, `/oauth/token`, `/oauth/revoke` | What the app's servers call |

The app's servers discover the authorization server from the public port, register themselves, and send your browser to the consent page. That page loads only on a device that is on your tailnet and signed in as an operator login. After approval the app talks only to the public port, with a short-lived access token that it refreshes on its own.

Funnel ports are limited to `443`, `8443` and `10000`, and exposure is port-wide. That is why the public paths get a port of their own and the consent page shares the private `8443`.

## Set up the host

The MCP HTTP transport must already run: `LIFEOS_MCP_BEARER_TOKEN` set and the `lifeos-mcp-http` unit enabled, as in [Agent Worker Setup](agent-worker-setup.md#step-3--enable-the-mcp-http-systemd-unit). Without the bearer token the transport does not start.

1. Add to `.env`, with your tailnet hostname and your Tailscale login:

   ```bash
   LIFEOS_OAUTH_OPERATOR_LOGINS=operator@example.com
   LIFEOS_OAUTH_ISSUER_URL=https://<host>.<tailnet>.ts.net:10000
   LIFEOS_OAUTH_AUTHORIZE_URL=https://<host>.<tailnet>.ts.net:8443/oauth/authorize
   LIFEOS_TAILSCALE_ALLOW_FUNNEL=true
   ```

   `tailscale status --json` shows your login under `User`. List only your own login: anyone listed can approve an app.

2. Add the routes to `config/tailscale-routes.local` (the commented block in `config/tailscale-routes.example` has them). Every route on `10000` carries `funnel=on`; the consent route on `8443` does not. A port that mixes the two is refused whole and kept private, so a declared private route never rides along on a public port.

3. Restart the transport and apply the routes:

   ```bash
   sudo systemctl restart lifeos-mcp-http
   ./scripts/setup-tailscale.sh
   ```

   `setup-tailscale.sh` does not read `.env`; its boot unit and the infra watchdog do. Run by hand, prefix it with `LIFEOS_TAILSCALE_ALLOW_FUNNEL=true`, or wait up to 5 minutes for the watchdog to publish the port.

4. Check: `tailscale funnel status` lists `:10000` as `Funnel on`, and `443` and `8443` as tailnet only.

The tailnet needs Funnel enabled for the node (the `funnel` node attribute in the Tailscale admin console). A freshly enabled Funnel port can take several minutes before public connections succeed.

## Connect an app

The connector URL is `https://<host>.<tailnet>.ts.net:10000/mcp`.

- **Claude** (web, desktop or mobile): Settings → Connectors → Add custom connector. Paste the URL; leave the OAuth client fields empty.
- **ChatGPT**: Settings → Apps & Connectors → Advanced settings → turn on Developer mode, then create a connector with the URL and OAuth authentication.

The app opens the consent page. Open it on a device that is on the tailnet and signed in as the operator login; it names the app, its redirect host and the tier. Approve. The connector then lists LifeOS's allowed tools.

## See and revoke connected apps

```bash
~/.venvs/lifeos/bin/python scripts/mcp_oauth.py list
~/.venvs/lifeos/bin/python scripts/mcp_oauth.py revoke <client_id>
```

Revoking deletes the app's registration and tokens at once; to reconnect, the app registers again and is approved again. Removing the connector inside the app does not revoke anything on the LifeOS side.

## Turn it off

- **Close public access:** set `LIFEOS_TAILSCALE_ALLOW_FUNNEL=false` and keep the route lines. The infra watchdog turns Funnel off on its next run and re-applies the routes privately. Deleting the lines instead leaves the port unmanaged, and an already-public port stays public until `tailscale funnel --https=10000 off`.
- **Disable OAuth entirely:** empty `LIFEOS_OAUTH_OPERATOR_LOGINS` and restart `lifeos-mcp-http`. Every OAuth endpoint then returns `404`, and issued tokens stop working.

## Troubleshooting

| Symptom | Cause |
|---|---|
| A public request from the LifeOS host itself fails the TLS handshake | A node cannot reach its own Funnel address. Test from a device off the tailnet. |
| Public requests fail the TLS handshake for a few minutes after enabling | The Funnel port is still propagating. |
| Consent page returns `403` | The device is off the tailnet, signed in as another login, or reached the page through the public port. |
| The app says the server needs authentication but never shows the consent page | `LIFEOS_OAUTH_ISSUER_URL` or `LIFEOS_OAUTH_AUTHORIZE_URL` names the wrong host or port; check `/.well-known/oauth-authorization-server`. |
| A route on `10000` is missing | `logs/infra-watchdog.log` names it; a mixed public/private port is reported by `setup-tailscale.sh`. |

## Related Documents

- [MCP OAuth](../specs/technical/mcp-oauth.md) — Endpoints, consent rules, tokens, the tool tier, residual risks
- [Operations — Host routes and dependencies](operations.md#host-routes-and-dependencies) — Declared routes, Funnel opt-in, the infra watchdog
- [Configuration](configuration.md#mcp-http-transport) — `LIFEOS_OAUTH_*` and `LIFEOS_MCP_*` settings
- [Agent Worker Setup](agent-worker-setup.md) — The bearer-token path for Managed Agents
