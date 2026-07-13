# Profit Protection Plan — Daily Profit Lock + Target Trading Window

**Status:** IMPLEMENTED in v9.10.0 (same branch/PR as this document). One
deliberate deviation from §3: `TRADE_WINDOW` ships **enabled by default**
(`04:00-07:30` ET) rather than opt-in via env var, per owner directive that the
bot "targets" this window every day — set `TRADE_WINDOW=""` to disable.
**Trigger event:** 2026-07-13 session — +60% by 6:00am ET, fully given back by 11am ET.

---

## 1. What the 2026-07-13 logs show

All log times below are UTC; ET = UTC−4 (July / EDT). Market tickers encode their
close time in **ET** (`-26JUL130430-` = 4:30am ET), which makes the pattern easy to read.

| UTC settle | ET market | Result | Daily realized P&L | Balance |
|---|---|---|---|---|
| (03:27 boot) | — | — | $0 | **$919.93** start |
| 08:31 | 4:30am | WIN +$192.08 | +$192 | $1,112 |
| 09:00 | 5:00am | WIN +$134.16 | +$326 | $1,246 |
| 10:00 | 6:00am | WIN +$227.24 | **+$553 peak (+60.2%)** | **$1,473.41** |
| 12:01 | 8:00am | LOSS −$219.98 | +$333 | $1,253 |
| 13:16 | 9:15am | LOSS −$219.52 | +$114 | $1,034 |
| 14:46 | 10:45am | LOSS −$219.87 | **−$106** | $814 → trough **$594.26** with $220 in flight |
| 15:15 | — | **container restart — all in-memory daily state wiped** (`PnL=$+0.00`, `WR=0/0`) | $0 (!) | $1,001.26 |

Three compounding observations:

1. **The edge is time-of-day concentrated.** All three wins were on the
   4:30–6:00am ET markets. All three losses were on the 8:00–10:45am ET markets.
   This is the same shape the per-bucket time-of-day prior was built for
   (US morning trends → post-open/late-morning mean-reverts), but the prior
   learns slowly; nothing halts a session that has already banked a windfall.
2. **The stake ramp amplifies the give-back.** The TEMP override ramps the hard
   stake +$10 per 2 wins, so the morning wins ($200 stakes) raised the stake to
   ~$220 exactly in time for the losing hours. Wins run at yesterday's size,
   losses at today's — a profit lock breaks that asymmetry mechanically.
3. **Any halt flag must survive restarts.** The 15:15 restart zeroed the daily
   P&L and win record. An in-memory `_profit_locked` would silently unlock on
   every Railway redeploy. Lock state must be persisted like
   `RECOVERY_STATE_PATH` / `PROBATION_STATE_PATH` already are.

---

## 2. Feature A — Daily Profit Lock (the halt trigger)

Halt **new entries** for the rest of the UTC trading day once the session's
realized P&L has either (a) hit a hard take-profit target, or (b) given back too
much of its intraday peak. Two triggers, one lock:

* **Hard target** — daily realized P&L ≥ `PROFIT_LOCK_TARGET_PCT` ×
  session-start balance → lock. "That's a great day, keep it."
* **Trailing give-back** — once daily peak P&L ≥ `PROFIT_LOCK_ARM_PCT` ×
  session-start balance the trail is *armed*; if P&L then falls below
  `peak × (1 − PROFIT_LOCK_GIVEBACK_PCT)` → lock. This catches the exact
  2026-07-13 shape (big spike in a small window, then decay) without capping a
  monster run that keeps going, and it works at *any* session window — the arm
  threshold is what makes "that kind of heights in a small session window"
  concrete: the faster the spike, the sooner it's protected.

### Config (all env-tunable, same style as existing risk controls)

```python
PROFIT_LOCK_ENABLED      = _env_bool("PROFIT_LOCK_ENABLED", True)
PROFIT_LOCK_TARGET_PCT   = _env_float("PROFIT_LOCK_TARGET_PCT", 0.40)   # hard take-profit
PROFIT_LOCK_ARM_PCT      = _env_float("PROFIT_LOCK_ARM_PCT", 0.15)      # trail arms at +15%
PROFIT_LOCK_GIVEBACK_PCT = _env_float("PROFIT_LOCK_GIVEBACK_PCT", 0.30) # keep ≥70% of peak
PROFIT_LOCK_STATE_PATH   = os.environ.get("PROFIT_LOCK_STATE_PATH", "profit_lock_state.json")
PROFIT_LOCK_PERSIST      = _env_bool("PROFIT_LOCK_PERSIST", True)
```

### Mechanics

* **P&L source is realized-only**: `paper_daily_pnl` (DEMO) /
  `live_daily_realized` (LIVE) — the same accumulators the daily summary and
  status snapshot already use. Never equity or cash delta: the v9.3.1 phantom
  daily-loss halt (an in-flight position's outlay read as a "loss") is exactly
  the bug class this avoids. An open position can never trip the lock.
* **Evaluate at the single choke point** where those accumulators are updated in
  `resolve_open_orders()` (bot.py ~2474 / ~2626): update `daily_pnl_peak`,
  then check hard target and trailing floor. On trip: set `_profit_locked`,
  persist, log `🔒 PROFIT LOCK`, send a Telegram alert (its own message —
  *not* `telegram_halt`, whose "HALTED (PERMANENT)" wording is wrong here:
  this lock is a good day and auto-clears at rollover).
* **New guard `profit_lock_check()`** in the guard stack next to
  `daily_loss_check()` in `run_decision()` (bot.py ~3088). It blocks **new
  entries only** — settlement polling, stale-order cleanup, heartbeats,
  Telegram, and the dashboard all keep running, and any in-flight position
  settles normally (its result still updates the P&L/peak, which matters if it
  loses after the lock… the lock is already on, nothing more to do).
* **State + persistence**: `{day, peak, locked}` written via the same
  atomic-JSON pattern as `RecoveryState` (`.tmp` + `os.replace`). On boot:
  if `state.day == today (UTC)`, restore peak and lock — this closes the
  restart hole demonstrated at 15:15. If the day differs, discard.
  (Same caveat as recovery: without a Railway Volume the file dies with the
  container; set `PROFIT_LOCK_STATE_PATH=/data/profit_lock_state.json` on a
  volume for real durability. Worth doing — this is the state file with the
  most money attached.)
* **Rollover**: `maybe_roll_session_day()` (bot.py ~3252) clears
  `_profit_locked`, resets `daily_pnl_peak = 0.0`, persists — alongside the
  `_session_halted` clear it already does. Next UTC day starts fresh.
* **Observability**: `write_status_snapshot()` gains `"profit_locked"` and
  `"daily_pnl_peak"`; `last_signal_desc = "profit locked (+$X)"` so the
  dashboard shows *why* it's idle; one INFO line per rejected evaluation,
  consistent with the other gates.

### Replay of 2026-07-13 with defaults

* Hard target = 0.40 × $919.93 = **$368**. Crossed at the 10:00 UTC settle
  (+$553). **Day ends locked at +$553 / balance $1,473** vs the actual +$81.
* Trailing (if the hard target were set higher): armed at +$326 (≥15%);
  floor = $553 × 0.70 = $387; the first loss (12:01, → +$333) trips it.
  **Locked at +$333.**
* Either trigger keeps $250–$470 that was actually given back, and the −$106
  trough never happens.

---

## 3. Feature B — Target trading window (the "1am–7:30am" question)

A hard clock window for new entries, defined in **exchange-local ET** so it
tracks DST (tickers are ET; the edge is an ET-session phenomenon, not a UTC one):

```python
TRADE_WINDOW    = os.environ.get("TRADE_WINDOW", "")           # "HH:MM-HH:MM", empty = disabled
TRADE_WINDOW_TZ = os.environ.get("TRADE_WINDOW_TZ", "America/New_York")
```

`trade_window_check()` sits in the guard stack beside `session_quality_check()`;
both must pass (the window narrows, never widens). Empty string = feature off,
current behavior unchanged. Supports overnight windows ("22:00-04:00") for free
by comparing minutes-of-day with wraparound.

### Advice: set it to `04:00-07:30`, not `01:00-07:30`

* The evidence window is **4:13–7:45am ET** — your own numbers, and the settle
  table above agrees (wins on 4:30/5:00/6:00am markets, losses on 8:00+).
* **1–4am ET (05:00–08:00 UTC) has never traded**: `SESSION_QUALITY` scores
  those hours 30/45/50, all below `MIN_SESSION_SCORE=60`, so the bot has zero
  data there. Even if the window said 1am, the session-quality gate would keep
  blocking until 4am ET anyway. Opening 1–4am requires deliberately raising
  those hour scores — do that as a separate experiment *after* a week of
  window-restricted days, not bundled into this change.
* Conversely, `SESSION_QUALITY` currently scores 12:00–16:00 UTC (8am–noon ET)
  at 80–95 — the exact hours that bled on 07-13 (and the per-bucket-prior
  comment block documents the same late-morning/afternoon mean-reversion
  pattern from June). The window gate settles that argument operationally
  without rewriting the table; the learned prior keeps accumulating evidence
  underneath either way.
* On 07-13 the window alone would have blocked all three losing entries
  (8:00/9:15/10:45am ET markets): **day ends +$553.**

### How A and B fit together

Independent gates, complementary jobs: the **window** is the opportunity filter
(only trade where the edge is proven), the **profit lock** is the risk filter
(when a session over-delivers, bank it — even *inside* the window; a +60% spike
by 6am locks before the 6–7:30am markets can decay it, and it also protects any
future window config that turns out to be wrong). Ship A first — it protects
every configuration — then B.

One caveat to size expectations: with `TRADE_WINDOW=04:00-07:30` the bot trades
~3.5h/day (~3–5 positions at the 60s cooldown + one-concurrent cap). The daily
slow-roll probation ramp re-arms every UTC midnight, so short sessions live
near the ramp floor longer. That is the intended trade — fewer, better trades —
but if throughput feels too thin, widen the window *end* (7:30→8:00am ET)
before touching the 1–4am side, since 8am is where the losses start.

---

## 4. Implementation checklist

1. **Config block** (bot.py, Risk controls section ~612): the 6 `PROFIT_LOCK_*`
   constants + `TRADE_WINDOW` / `TRADE_WINDOW_TZ`.
2. **State** (globals ~857): `daily_pnl_peak: float = 0.0`,
   `_profit_locked: bool = False`; load persisted state on boot in `main()`.
3. **Peak/trip evaluation** in `resolve_open_orders()` — one helper
   `update_profit_lock(daily_realized, session_start_balance)` called from both
   the DEMO and LIVE settle paths.
4. **Guards**: `profit_lock_check()` + `trade_window_check()` in the guard
   stack (~2791) and wired into `run_decision()` (~3088), with gate-style INFO
   logs and `last_signal_desc`.
5. **Rollover** in `maybe_roll_session_day()`: clear lock, zero peak, persist.
6. **Telegram**: `telegram_profit_lock(pnl, pct, balance, trigger)` — 🔒
   framing, states it auto-resumes at UTC rollover.
7. **Snapshot**: `profit_locked`, `daily_pnl_peak` fields; dashboard picks them
   up from the JSON automatically.
8. **Tests** (test_bot.py):
   * hard-target trip at exactly the threshold; no trip just below
   * trail arms at `ARM_PCT`, trips on give-back, never trips unarmed
   * open-position outlay cannot trip the lock (regression, v9.3.1 class)
   * lock blocks `run_decision` entry path; resolution still processes
   * rollover clears lock + peak; boot restores same-day lock from disk,
     discards stale-day state
   * window: inside/outside/boundary, overnight wraparound, disabled-when-empty,
     DST honored via `zoneinfo`
9. **Docs**: README env-var table + `TRADING_DOCTRINE.md` note; Railway env
   vars (`TRADE_WINDOW=04:00-07:30`; volume-backed `PROFIT_LOCK_STATE_PATH`).

### Defaults to deploy with

| Var | Value | Meaning on a $1,000 session |
|---|---|---|
| `PROFIT_LOCK_TARGET_PCT` | 0.40 | lock the day at +$400 realized |
| `PROFIT_LOCK_ARM_PCT` | 0.15 | trail arms at +$150 |
| `PROFIT_LOCK_GIVEBACK_PCT` | 0.30 | after arming, never give back >30% of peak |
| `TRADE_WINDOW` | `04:00-07:30` (ET) | the proven edge window |

Open question for tuning (not blocking): whether `PROFIT_LOCK_TARGET_PCT=0.40`
is too generous once the window is live — inside a 3.5h window a 0.25–0.30
target locks most sessions that spike. Start at 0.40, review after a week of
locked-day logs.
