# Railway setup — consolidated checklist

Everything to configure in Railway for the **dashboard + monitoring + Telegram
control**, in one place. You run **two services from this one repo**:

| Service | Start command (via config file) | Purpose |
|---|---|---|
| **bot** (existing) | `railway.toml` → `python bot.py` | your headless trader — unchanged |
| **dashboard** (new) | `railway.dashboard.toml` → gunicorn | multi-tenant site + monitoring + Telegram control |

---

## A. Create / point the dashboard service
1. Railway → project → **New → GitHub Repo → `markeymachine`** (a 2nd service).
2. Dashboard service → **Settings → Config-as-code / "Railway Config File"** →
   set to **`railway.dashboard.toml`**.
3. Dashboard service → **Settings → Networking → Generate Domain**. (No port in
   the URL; if asked for a target port, use `8080`.)
4. **Add a Volume** to the dashboard service, mount path **`/data`**.

## B. Dashboard service — Variables (one pass)

| Variable | Required? | Value / meaning |
|---|---|---|
| `DASHBOARD_SECRET_KEY` | **yes** | long random string (signs login cookies) |
| `DASHBOARD_DATA_DIR` | **yes** | `/data` (the Volume mount — persists users, accounts, keys, logs) |
| `DASHBOARD_COOKIE_SECURE` | yes | `true` (you're on HTTPS) |
| `ADMIN_EMAILS` | yes | your email (comma-separated for more) — grants the Admin/Logs tabs |
| `MONITOR_TOKEN` | recommended | long random string → enables `GET /health?token=…` |
| `DASHBOARD_TELEGRAM_BOT_TOKEN` | recommended | operator bot from @BotFather (alerts **and** commands) |
| `DASHBOARD_TELEGRAM_CHAT_ID` | recommended | your chat id (comma-separated for more authorized chats) |
| `WATCHDOG_INTERVAL` | optional | watchdog poll seconds (default `60`) |
| `WATCHDOG_STALE_SECS` | optional | "no update" → stalled (default `180`) |
| `WATCHDOG_AUTORESTART` | optional | auto-restart crashed workers (default `true`) |
| `WATCHDOG_MAX_RESTARTS` | optional | auto-restarts/account/hour (default `3`) |
| `DASHBOARD_WATCHDOG` | optional | master watchdog on/off (default `true`) |
| `DASHBOARD_TELEGRAM_COMMANDS` | optional | command listener on/off (default `true`) |
| `DASHBOARD_ALLOW_LIVE` | **leave unset** | Phase-1 paper safety; setting `true` would permit live — do **not** until Phase 2 |

> The **bot** service keeps its own existing variables (`KALSHI_API_KEY_ID`,
> `KALSHI_PRIVATE_KEY_PEM`, sizing, etc.). Nothing there changes.

## C. Telegram (one operator bot does alerts **and** commands)
1. Create a bot via **@BotFather** → copy its token → set
   `DASHBOARD_TELEGRAM_BOT_TOKEN`.
2. Message your new bot once, then get your chat id (e.g. via `@userinfobot`) →
   set `DASHBOARD_TELEGRAM_CHAT_ID`.
3. After redeploy, send the bot **`/help`**. You'll get the command list:
   `/status`, `/logs`, `/analyze`, `/format`, `/set`, `/pause`, `/resume`,
   `/restart`, `/confirm`, `/cancel`. (Changes require `/confirm`.)

## D. For Claude to read logs on demand (optional)
- Add `RAILWAY_API_TOKEN` (+ `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`,
  `RAILWAY_SERVICE_ID`) as **secrets in the Claude Code web environment** (not in
  Railway) so `tools/railway_logs.py` works.
- And widen the Claude web-environment **network policy** to allow your dashboard
  domain (`*.up.railway.app`) and `backboard.railway.app` — by default Claude's
  environment blocks outbound internet, so it can't reach these without that.
  (Your `/health` + Telegram alerting work regardless.)

## E. Redeploy & verify
1. Redeploy the dashboard service.
2. Open the domain → **Sign up** → Settings (Kalshi key) → Formats → **Start**.
3. `https://<domain>/health?token=<MONITOR_TOKEN>` returns JSON.
4. Telegram `/status` replies. Trigger nothing-changed = silence; crash/stall =
   an alert.

See also: [`DASHBOARD_SETUP.md`](DASHBOARD_SETUP.md) (walkthrough) and
[`MONITORING.md`](MONITORING.md) (monitoring + commands detail).
