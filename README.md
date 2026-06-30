# MarkeyMachine v5.3.0

> Production quant bot for Kalshi 15-minute BTC up/down prediction markets.
> Near-money order book pressure + BTC momentum confirmation + fractional Kelly sizing.

---

## Strategy

### Signal 1 — Near-Money OB Pressure (primary edge)
Measures dollar depth within ±10 cents of the current mid-price on the YES/NO order book. If sufficient imbalance exists on one side, smart money is positioned there. Requires ≥$5 total depth to prevent single resting orders from manufacturing false signals.

**v5.3.0: Adaptive threshold** — thin books ($5-15 depth) require 70%+ imbalance, thick books ($50+ depth) allow 58%+, medium books use profile default (62%).

### Signal 2 — BTC Momentum Confirmation
Fetches live BTC/USD price from Kraken (Coinbase fallback). If BTC moved ≥0.20% in the same direction as the OB signal in the last 2 minutes → AGREE (boosts win probability). If BTC moved against the OB signal → CONFLICT (trade skipped). Flat market → NEUTRAL (no adjustment).

### Signal 3 — Price Breakeven Guard
Only enters contracts priced ≤67 cents. At 68.5% historical win rate, the mathematical breakeven is 68 cents. This ensures positive expected value on every trade.

### Signal 4 — Bias Filter
Skips contracts priced <35 cents or >65 cents. Academic research (Bürgi et al. 2025) confirms Kalshi contracts below ~20 cents lose ~60% of capital on average.

### v5.3.0: OB Trend Detection
Compares the current order book snapshot to the previous one for each ticker. Trades only fire when pressure is building or stable — if the imbalance is fading (dropped >5%) or the direction flipped, the trade is skipped. Prevents entering on stale or collapsing signals.

### v5.3.0: Multi-Market Scanner
Scans ALL open BTC markets across KXBTC15M/KXBTCD/KXBTC series and evaluates each for signals. The bot tries every valid market per cycle instead of just picking the one closest to 50c. Position guard and cooldown prevent over-trading.

### Sizing — Flat Stake with Recovery Mode *(v9.5.0)*
`f* = (b×p - q) / b` where b = net odds, p = OB win probability, q = 1-p.
Kelly is used **only as an edge gate** (`f* > 0` ⇒ positive expectancy). The stake is **flat on every qualifying trade, regardless of balance** — no Kelly or balance-fraction down-scaling; the only clamp is cash on hand.

The stake is **derived from a persistent mode** via `active_trade_size()`:

- **Normal mode** → `NORMAL_TRADE_SIZE` (default $500; falls back to legacy `TRADE_SIZE_DOLLARS`).
- **Recovery mode** → `RECOVERY_TRADE_SIZE` (default $100). Activated when a full-size trade settles a loss; the recovery target is the realized balance *immediately before that trade*. The bot trades at the reduced size until balance climbs back to the target, then auto-resumes full size.

Recovery state `{active, target}` is persisted atomically and reconciled on boot, so it survives an in-container restart and can never wedge. See [`TRADING_DOCTRINE.md`](TRADING_DOCTRINE.md) §5 for the full lifecycle and edge-case guarantees. **For redeploy-durable recovery state on Railway, mount a Volume and set `RECOVERY_STATE_PATH` to a path on it.**

- **Daily slow-roll ramp** *(v9.7.0)* → the first trade of **every** UTC trading day re-enters the probation ramp at the floor and climbs `$100 → $250 → $500` (the same `RECOVERY_TRADE_SIZE → … → NORMAL_TRADE_SIZE` rungs used after a recovery exit). It advances one rung on a `PROBATION_WIN_STREAK`-win streak **or** a `≥PROBATION_WIN_RATE_MIN` rolling win rate, steps down a rung on a loss, and graduates to full size at the top. This keeps the bot from opening a fresh day cold at full size. The re-arm is **skipped while Recovery is active** (the deeper claw-back tier wins), and a restart that crosses midnight re-arms on boot via a persisted arm-date. Disable with `PROBATION_RAMP_ENABLED=false` (every day then stays full size).

- **Balance-gated $1000 ceiling** *(v9.8.0)* → set `TRADE_SIZE_DOLLARS=1000` to raise the ramp ceiling; the ladder auto-builds in `$250` steps to `$100 → $250 → $500 → $750 → $1000`. Stakes **above `HIGH_STAKE_GATE_SIZE` (default $500)** require account equity **≥ `HIGH_STAKE_MIN_BALANCE` (default $5000)**. The gate is enforced twice: a hard ceiling re-checked **every trade** at sizing time (so a balance that dips back below the line caps the next stake to `$500`), and at **ramp-advance time** so a win rate banked at `$500` cannot jump straight to `$1000` the instant balance crosses the line — the high rungs are earned one at a time. Below `$5000` the effective ceiling stays `$500`. The step size is `PROBATION_RUNG_STEP` (default `250`); an explicit `PROBATION_RUNGS` override is still honored.

### Execution — Maker Limit Orders
Posts limit orders one cent inside the best bid/ask. Kalshi makers pay zero fee. Takers pay ~1% of winnings. Fee drag on taker orders: ~$5+/day at scale.

**v5.3.0: Stale order cancellation** — unfilled maker orders are automatically canceled after 5 minutes (configurable) to free capital for better opportunities.

### Sizing — Laddering Stake Overlay *(opt-in)*
A performance-driven overlay on the Kelly stake that scales trade size by a multiplier (0.5×–2×) based on the win rate over a rolling window of recent trades. Sizes up when the edge is paying off, demotes/pauses on losing streaks and daily drawdown, and enforces an anti-chase cooldown after every loss. **Adapts stake size only — never the strategy or signals.** Disabled by default; set `LADDER_ENABLED=true` to activate. See [`LADDER_STRATEGY.md`](LADDER_STRATEGY.md) and [`ladder.py`](ladder.py).

---

## Trading Formats

The bot runs **one strategy** (trend-confirming order-book pressure); what changes between deployments is the *posture* — sizing, gate strictness, and which overlays (Ladder, Recovery, Probation) are on. A **Trading Format** bundles all of those settings under one name so the whole posture switches with a single knob. Pick one with the `TRADING_FORMAT` env var (or the dashboard dropdown):

```bash
TRADING_FORMAT=conservative python bot.py
python bot.py --list-formats        # show all formats and their settings
python formats.py                   # same listing, no credentials needed
```

| Format | Intent |
|---|---|
| `conservative` | **Capital preservation.** Strict gates, recovery + probation on, ladder off, smaller stake, earlier session-stop. Fewest, highest-conviction trades. |
| `balanced` *(default)* | The shipped v9.6 production posture: two-tier Recovery + Probation sizing, ladder off, doctrine entry thresholds. |
| `aggressive` | **Edge hunter.** Looser gates, two concurrent positions, larger stake, Ladder overlay on (up to 2×). Highest throughput and variance. |
| `recovery_first` | **Drawdown guard.** Small stake, strict gates, recovery/probation emphasized with a longer post-recovery pause. Built to rebuild after a rough stretch. |

A format only seeds **defaults** (`os.environ.setdefault`): any environment variable you set explicitly always wins, so you can pick a format and still override an individual knob (e.g. `NORMAL_TRADE_SIZE` to size to your bankroll). All formats default to `DEMO_MODE=true` (paper) — going live is an explicit `DEMO_MODE=false`. See [`formats.py`](formats.py).

---

## Management Dashboard

A lightweight Flask control panel ([`dashboard/`](dashboard/)) to pick a Trading Format, start/stop the worker, and watch live status (balance, P&L, win rate + Wilson CI, active sizing mode, open positions) — instead of editing env vars by hand. It runs `bot.py` as an isolated worker per account, so it is **single-account today but multi-tenant-ready**: the account store is a list and every worker gets its own state directory.

```bash
pip install -r requirements.txt
DASHBOARD_PASSWORD=yourpassword python -m dashboard.app   # http://localhost:8080
```

- **Default PAPER everywhere.** Switching an account to LIVE (real money) requires typing a confirmation in Settings.
- Kalshi credentials are entered in the GUI; the private key is stored under the account's own directory and never echoed back. **Client funds stay in the client's own Kalshi account** — the dashboard only needs a trade-scoped API key.
- **The existing headless bot is unchanged.** `railway.toml` still starts the trader with `python bot.py`. Run the dashboard as a **separate** Railway service pointed at its own config file [`railway.dashboard.toml`](railway.dashboard.toml) (Settings → Config-as-code → set the config-file path), which starts `python -m dashboard.app`. A UI "Custom Start Command" alone is **not** enough — Railway's config-in-code overrides it, so the bot's `railway.toml` would otherwise run on the dashboard service and crash it. Full walkthrough: [`DASHBOARD_SETUP.md`](DASHBOARD_SETUP.md). Selecting the default `balanced` format — or running with no `TRADING_FORMAT` — does not alter the bot's sizing or any trading logic.

See [`BUSINESS_PLAN.md`](BUSINESS_PLAN.md) for the managed-service model this enables.

---

## Risk Controls

| Control | Behavior |
|---|---|
| **Recovery mode** *(v9.5.0)* | After a full-size loss, drops to `RECOVERY_TRADE_SIZE` until balance recovers to the pre-loss level, then auto-resumes `NORMAL_TRADE_SIZE`. Persistent across restarts |
| **Streak filter** *(only active auto-hold)* | After `MAX_CONSEC_LOSSES` (3) consecutive losses, pauses trading for `STREAK_PAUSE_SECS` then resets the counter |
| Session stop *(catastrophic backstop)* | Halts if balance drops below `SESSION_STOP_FRACTION` (40%) of session-start balance |
| Position guard | One entry per market ticker, no re-entry until expiry |
| Expiry guard | Skips contracts priced >85c or <15c (near-certain outcome, zero EV) |
| Spread guard | Skips zero/crossed spreads (broken book) |

> **Removed (v9.4.0, owner directive):** the % and $ daily-loss caps, the
> balance floor, and RECOVERY mode (10% drawdown sizing cut). The consecutive-loss
> streak pause is the only active auto-hold; the 40% session stop is retained
> only as a catastrophic backstop.
| **Liquidity filter** *(v5.3.0)* | Skips low-liquidity UTC hours (default 4-8 UTC / midnight-4am ET) |
| **Concurrent limit** *(v5.3.0)* | Max simultaneous open positions (default 2) |
| **Stale cancel** *(v5.3.0)* | Auto-cancels unfilled orders after timeout (default 300s) |

---

## v5.3.0 — Win Rate Confidence Tracking

The bot now computes Wilson score confidence intervals on accumulated win/loss data. This tells you whether your edge is statistically real or just luck:

- **Heartbeat** (every 15 min): includes WR% with 95% CI when ≥5 trades resolved
- **Daily summary**: includes confidence interval
- **Logs**: portfolio status includes CI bounds

Example: `WR: 72.0% [58-83%]` means your true win rate is somewhere between 58% and 83% with 95% confidence. As you accumulate more trades, the interval narrows.

---

## v5.3.0 — Smarter Paper Mode

Paper mode no longer uses a random 68.5% coin flip to determine outcomes. Instead:
- Each paper order records the BTC price at entry
- At resolution (15 min later), the bot checks current BTC price
- If you bought YES and BTC went up → WIN. If BTC went down → LOSS.
- If you bought NO and BTC went down → WIN. If BTC went up → LOSS.
- Falls back to RNG only if BTC price feed is unavailable.

This gives paper mode results that correlate with actual market outcomes.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main trading bot — runs as the worker |
| `formats.py` | Trading Format presets (`TRADING_FORMAT`) |
| `dashboard/` | Flask management dashboard (run/monitor/select format) |
| `ladder.py` | Opt-in performance stake overlay |
| `telegram_utils.py` | Telegram notification module |
| `test_bot.py` / `test_formats.py` / `test_dashboard.py` / `test_ladder.py` | Pytest suites |
| `BUSINESS_PLAN.md` | Managed-service business plan |
| `requirements.txt` | Python dependencies |
| `Procfile` / `railway.toml` | Deployment config |

---

## Testing

```bash
pip install -r requirements.txt
pytest test_bot.py -v
```

Tests cover all risk controls (P0), signal math (P1), and v5.3.0 features (P2). 60+ test cases including edge boundaries, adaptive threshold scaling, OB trend detection, stale order cancellation, and Wilson confidence intervals.

---

## Setup

### Step 1 — Kalshi RSA API Keys
1. Log into kalshi.com → Settings → API Keys → Create New Key
2. Save the Key ID (UUID format) and download the PEM file
3. The PEM looks like: `-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----`

### Step 2 — GitHub Repo
Upload all files to a new GitHub repo. Commit to `main`.

### Step 3 — Railway
1. New Project → Deploy from GitHub Repo → select your repo
2. Variables tab → add all variables below

### Step 4 — Telegram Bot (optional but strongly recommended)
1. Message @BotFather on Telegram → `/newbot` → follow prompts → save the token
2. Message your bot once, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Copy the `chat.id` value

---

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `KALSHI_API_KEY_ID` | required | UUID from Kalshi Settings → API Keys |
| `KALSHI_PRIVATE_KEY_PEM` | required | Full PEM. Replace newlines with `\n` if needed |
| `DEMO_MODE` | `true` | Set `false` for live trading |
| `TRADER_MODE` | `quant` | Only `quant` is recommended for live |
| `NORMAL_TRADE_SIZE` | `TRADE_SIZE_DOLLARS` | Full-mode per-trade stake (v9.5.0). Defaults to `TRADE_SIZE_DOLLARS` so existing configs keep working |
| `RECOVERY_TRADE_SIZE` | `100` | Reduced stake used while recovering a full-size loss (v9.5.0) |
| `RECOVERY_STATE_PATH` | `recovery_state.json` | Where recovery state is persisted. Point at a mounted Railway **Volume** (e.g. `/data/recovery_state.json`) to survive redeploys |
| `RECOVERY_PERSIST` | `true` | Set `false` to disable recovery-state persistence |
| `RECOVERY_LADDER_PAUSE_TRADES` | `5` | After recovery exits and sizing returns to `NORMAL_TRADE_SIZE`, hold the ladder's win-rate size-up at baseline for this many fresh trades (win or loss) before it can scale above normal again. `0` disables. No effect unless `LADDER_ENABLED=true` |
| `TRADE_SIZE_DOLLARS` | `500` | Legacy flat stake; now the default for `NORMAL_TRADE_SIZE`. Set `1000` to raise the slow-roll ceiling (v9.8.0) |
| `HIGH_STAKE_MIN_BALANCE` | `5000` | *(v9.8.0)* Equity required before stakes above `HIGH_STAKE_GATE_SIZE` (e.g. $750/$1000) unlock. Below it the stake is capped at the gate size |
| `HIGH_STAKE_GATE_SIZE` | `500` | *(v9.8.0)* Stakes above this dollar size are balance-gated by `HIGH_STAKE_MIN_BALANCE` |
| `PROBATION_RUNG_STEP` | `250` | *(v9.8.0)* Dollar step for the auto-built ramp ladder (floor → … → `NORMAL_TRADE_SIZE`) |
| `MAX_BET_FRACTION` | `1.0` | **Dead config (v9.4.1)** — flat sizing ignores it |
| `SESSION_STOP_FRACTION` | `0.40` | Catastrophic backstop — halt below this fraction of session-start balance |
| `YES_BREAKEVEN_PRICE` | `67` | Skip contracts above this price (cents) |
| `KELLY_FRACTION` | `0.30` | v9.4.1: only gates edge (`>0`); no longer scales stake size |
| `MAX_CONSEC_LOSSES` | `3` | Streak pause threshold — the only active auto-hold |
| `PAPER_BALANCE` | `25.0` | Starting balance in paper mode |
| `POLL_INTERVAL_SECS` | `30` | Market scan frequency |
| `TELEGRAM_BOT_TOKEN` | optional | From @BotFather |
| `TELEGRAM_CHAT_ID` | optional | Your Telegram chat ID |
| `STALE_ORDER_TIMEOUT` | `300` | *(v5.3.0)* Seconds before canceling unfilled orders |
| `MAX_CONCURRENT_POS` | `2` | *(v5.3.0)* Max simultaneous open positions |
| `LOW_LIQ_START_UTC` | `4` | *(v5.3.0)* Low-liquidity skip window start (UTC hour) |
| `LOW_LIQ_END_UTC` | `8` | *(v5.3.0)* Low-liquidity skip window end (UTC hour) |

---

## Telegram Alerts

| Event | Fires |
|---|---|
| Boot | Always (includes balance, caps, version, new v5.3.0 params) |
| Heartbeat | Every 15 minutes (balance, P&L, open orders, WR + confidence interval) |
| Trade entered | Every live order placed (OB%, edge%, cost) |
| WIN | Every settled winning trade |
| LOSS | Every settled losing trade (live mode only) |
| HALT | Session stop, daily loss cap, balance floor |
| Daily summary | Midnight UTC (~8pm ET) with confidence intervals |
| Shutdown | On manual stop |

---

## Version History

| Version | Key Changes |
|---|---|
| **v5.3.0** | **Multi-market scanner; stale order cancellation (5 min); adaptive OB threshold (thin/thick books); time-of-day liquidity filter; OB trend detection (building/fading); smarter paper mode (BTC movement, not RNG); Wilson confidence intervals; concurrent position limit; 60+ pytest tests** |
| v5.2.1 | BOT_VERSION tag; BTC feed timed backoff; global consecutive_losses fix; WIN notification branch fix |
| v5.2.0 | No Kalshi mid proxy in BTC feed; OB depth floor $5; streak filter deadlock fix; running_pnl |
| v5.1.x | Telegram heartbeat + entry/loss alerts; positions endpoint for resolution; stale order cleanup |
| v5.0 | Session stop (50% halt); BTC momentum 0.20% threshold; spread guard fixed; Kelly 35% |
| v4.0 | win_prob = OB imbalance only; balance floor; paper mode fully simulated |
| v3.0 | RSA-PSS auth; near-money OB filter ±10c; maker limit orders; bias filter |

---

## Risk Disclosures

- All trading involves risk of capital loss.
- The 15-minute BTC markets on Kalshi launched December 2025 — historical data is limited.
- Past win rates do not guarantee future performance.
- Start with DEMO_MODE=true and verify behavior before trading real money.
- Set MAX_DAILY_LOSS_DOLLARS conservatively relative to your account size.
- The Wilson confidence intervals help you evaluate whether your edge is real, but statistical significance does not guarantee future performance.
