# Monitoring & log access

Two layers, by design:

1. **Always-on (for you).** The dashboard watches every customer's worker and
   **alerts you on Telegram in real time** when one crashes, stalls, or
   recovers — and can auto-restart a crashed worker. This runs 24/7 and does
   **not** depend on Claude being online.
2. **On-demand (for Claude / any uptime check).** A token-protected `/health`
   JSON and an admin log viewer expose every worker's status and recent log
   lines off the public URL. A helper script can also pull raw Railway logs.

> **Honest limit:** Claude sessions are ephemeral and there is no Railway→Claude
> wake trigger. Claude cannot watch your logs 24/7 on its own. The always-on
> Telegram alerting is what covers you when Claude isn't in a session; when you
> *do* bring Claude in, it reads the surfaces below and reacts.

---

## 1. Real-time alerts (watchdog → Telegram)

The dashboard runs a background watchdog (`dashboard/watchdog.py`). It classifies
each worker as **healthy / stalled / crashed / stopped** and sends an operator
Telegram message on every state change (not on every poll, so no spam).

Set on the **dashboard** Railway service → Variables:

| Variable | Purpose | Default |
|---|---|---|
| `DASHBOARD_TELEGRAM_BOT_TOKEN` | Operator alert bot (from @BotFather) | — |
| `DASHBOARD_TELEGRAM_CHAT_ID` | Your chat id | — |
| `WATCHDOG_INTERVAL` | Poll seconds | `60` |
| `WATCHDOG_STALE_SECS` | "no status update" → stalled | `180` |
| `WATCHDOG_AUTORESTART` | Auto-restart crashed workers | `true` |
| `WATCHDOG_MAX_RESTARTS` | Auto-restarts per account per hour | `3` |
| `DASHBOARD_WATCHDOG` | Master on/off | `true` |

If the Telegram vars are unset, alerts are simply skipped (the watchdog still
auto-restarts and logs).

---

## 2. `/health` endpoint (Claude / uptime monitors read this)

`GET https://<dashboard-domain>/health?token=<MONITOR_TOKEN>`
(or send the token as the `X-Monitor-Token` header).

- Set `MONITOR_TOKEN` (a long random string) on the dashboard service. Without
  it the endpoint is **404** (never exposed unauthenticated). A wrong token is
  **401**.
- Returns `ok` plus, per account: `state`, `running`, `balance`, `session_pnl`,
  `active_mode`, `last_signal`, `updated_at`, `stale_seconds`, and (for
  crashed/stalled workers) `last_error` — the error/traceback lines tailed from
  that worker's log.
- Provider-agnostic: this is just your public URL, so it works for Claude, for
  an uptime service (UptimeRobot etc.), or a `curl`.

## 3. Admin log viewer

Logged in as an admin (`ADMIN_EMAILS`):
- **Logs** tab (`/admin/logs`) — last lines of every worker's log.
- `GET /admin/api/logs?account=<id>&lines=N` — JSON tail for a specific worker,
  for Claude to pull.

---

## 4. Raw Railway logs (`tools/railway_logs.py`)

For the dashboard service's own process logs, your separate live-bot service, or
build/deploy logs (things not in a per-account worker log).

Add these as **environment secrets in the Claude Code web environment** for this
repo (so they persist across Claude sessions — they are never committed):

- `RAILWAY_API_TOKEN` — a Railway account/team or project token
- `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_SERVICE_ID`

Then, in a session: `python tools/railway_logs.py --lines 100`.

---

## Reaching these from Claude — network policy ⚠️

For Claude to read `/health`, `/admin/api/logs`, or run `tools/railway_logs.py`,
**Claude's web-environment network policy must allow outbound HTTPS** to the
relevant hosts. The default locked-down policy blocks general internet egress
(only package registries are allowed), so by default Claude **cannot** reach your
dashboard domain or the Railway API.

To enable on-demand reading, choose a Claude Code environment **network policy**
that allows at least:
- `*.up.railway.app` (or your custom dashboard domain) — for `/health` + logs
- `backboard.railway.app` — for raw Railway logs

See https://code.claude.com/docs/en/claude-code-on-the-web for the environment /
network-policy settings. **This does not affect the always-on Telegram
alerting**, which runs inside Railway and reaches Telegram regardless.
