# Dashboard Setup (Railway) — step by step

The management dashboard runs as a **second Railway service** in the same
project as your bot. Your existing bot service is not touched.

> **Why a special config file?** Both services deploy from this one repo.
> Railway's config-in-code overrides the dashboard UI's "Custom Start Command",
> so the bot's `railway.toml` (`python bot.py`) would also run on the dashboard
> service and crash it (the bot needs Kalshi credentials). The dashboard service
> is therefore pointed at its own config file, `railway.dashboard.toml`, which
> starts `python -m dashboard.app`.

## 1. Create the service
- Railway → your project → **New → GitHub Repo → `markeymachine`**.
  This adds a second service alongside the bot.

## 2. Point it at the dashboard config (the important step)
- Open the new service → **Settings → Config-as-code** (a field named
  **"Railway Config File"** / config file path).
- Set it to:  `railway.dashboard.toml`
- Save. This makes the service run `python -m dashboard.app` instead of the bot.

## 3. Add variables
The dashboard is **multi-tenant**: customers sign up themselves with an email +
password, and each one is isolated (their own account, their own Kalshi key,
their own paper bot). There is **no shared password** anymore.

New service → **Variables**:
- `DASHBOARD_SECRET_KEY` = a long random string (signs login cookies — keep it
  secret and stable).
- `DASHBOARD_COOKIE_SECURE` = `true` (you're on HTTPS via the Railway domain).
- `ADMIN_EMAILS` = your email (comma-separated for more). Those users get an
  **Admin** tab showing every account.
- **Add a Volume** and set `DASHBOARD_DATA_DIR` = `/data` (volume mount path
  `/data`). This is **strongly recommended** — it's where customer logins,
  accounts, and keys live; without it they reset on every redeploy.

> **Phase 1 is paper-only.** Live (real-money) trading is intentionally disabled
> for everyone — there is no `DASHBOARD_ALLOW_LIVE` flag set, so every customer
> bot runs in paper regardless. Do not enable live until the legal/compliance and
> key-encryption work (Phase 2) is done.

## 4. Domain & port
The dashboard is served by **gunicorn** (a production web server), bound to the
`PORT` Railway injects. Railway routes the public domain to that port for you.

- New service → **Settings → Networking → Generate Domain**.
- **You do not put a port in the URL.** Open `https://<your-domain>` directly.
- If the Generate-Domain dialog asks which **target port** to expose, enter
  `8080` (the dashboard's fallback when `PORT` isn't set) — or, if you set a
  `PORT` variable yourself, use that value.

### "Application failed to respond"
This means the container is up but the domain isn't reaching the web port. Fix:
1. Open the dashboard service's **Deploy logs**. You should see a line like
   `Listening at: http://0.0.0.0:8080` (or another number). If instead you see
   the bot's banner or a `KALSHI_API_KEY_ID` error, the config file from step 2
   isn't applied — recheck **Settings → Config-as-code = `railway.dashboard.toml`**
   and redeploy.
2. If gunicorn is listening but the page still fails: **Settings → Networking**,
   delete the existing domain, then **Generate Domain** again so Railway
   re-detects the live port. (A domain generated while the service was crashing
   can keep a stale target port.)
3. As a deterministic option, add a **Variable** `PORT` = `8080`, set the
   domain's target port to `8080`, and redeploy.

## 5. Use it (you and your customers)
1. Open the domain. You and each customer **Sign up** with an email + password.
2. **Settings** → enter the Kalshi **API Key ID** + paste the **Private Key
   (PEM)**, set a paper starting balance, optional Telegram → **Save**.
   (Each customer's funds stay in their own Kalshi account; the key can be
   revoked from Kalshi at any time.)
3. **Formats** → pick one (start with **Conservative** or **Balanced**).
4. **Dashboard** → **Start**. It runs in **PAPER** (practice) mode — no real
   money. Status cards fill in within a minute or two.
5. As an admin (`ADMIN_EMAILS`), the **Admin** tab lists every account and its
   status.

## Safety / Phase-1 limits
- **Paper only.** Live trading is disabled site-wide in Phase 1, so the dashboard
  cannot place real orders — it can run safely alongside your separate live bot
  service without any risk of double-trading.
- **Customer keys are stored on disk (not yet encrypted).** Fine for a small
  paper beta; this must become encryption-at-rest / a secrets manager before any
  real-money/live phase. Use a Volume and keep `DASHBOARD_SECRET_KEY` private.
- After a Railway redeploy, each running worker may need **Start** clicked again.
