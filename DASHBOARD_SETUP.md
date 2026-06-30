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
New service → **Variables**:
- `DASHBOARD_PASSWORD` = a login password you choose
- `DASHBOARD_SECRET_KEY` = any long random string
- *(recommended)* add a **Volume**, then `DASHBOARD_DATA_DIR` = `/data`
  (set the volume's mount path to `/data`) so your accounts/settings survive
  redeploys.

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

## 5. Use it
1. Open the domain → log in with `DASHBOARD_PASSWORD`.
2. **Settings** → enter your Kalshi **API Key ID** + paste the **Private Key
   (PEM)**, set a paper starting balance, optional Telegram → **Save**.
3. **Formats** → pick one (start with **Conservative** or **Balanced**).
4. **Dashboard** → **Start**. It runs in **PAPER** by default (no real money).
   Status cards fill in within a minute or two.
5. Going live later: **Settings → Trading mode → type `LIVE` → Restart**.

## Safety
- ⚠️ **Never run two LIVE bots on the same Kalshi account.** Keep the dashboard
  in **paper** while your existing bot trades live. To run the dashboard live,
  stop/delete the standalone bot service first.
- After a Railway redeploy you may need to click **Start** again to relaunch the
  worker.
