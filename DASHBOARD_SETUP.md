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
- New service → **Settings → Networking → Generate Domain**.
- **You do not put a port in the URL.** The app listens on the `PORT` Railway
  injects automatically, and the domain maps to it. If the Generate-Domain
  dialog asks which port to expose, use the one Railway has detected (it appears
  once the app is running); if nothing is detected yet, type `8080`.
- If the page doesn't load, it's almost always because the service isn't running
  (check the Deploy logs) — fix the start command (step 2) and redeploy.

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
