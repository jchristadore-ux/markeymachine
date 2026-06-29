# Laddering Stake Management

> Dynamic, performance-driven stake sizing for the Kalshi 15-minute BTC Up/Down
> bot. Adapts **stake size only** — it never touches signal generation or
> strategy. Implemented in [`ladder.py`](ladder.py), wired into `bot.py` as an
> opt-in overlay on the existing Kelly stake.

---

## 1. Why

The bot's edge is probabilistic, not guaranteed. Win rate drifts intraday with
liquidity, volatility, and regime. A flat stake leaves money on the table when
the edge is paying off and bleeds capital when it isn't. The ladder makes one
job — *how much to risk per trade* — responsive to a rolling read of recent
performance, while a layer of hard guardrails caps the downside of variance.

The ladder is **deterministic**: given the same trade history and clock, it
returns the same stake. No randomness anywhere.

---

## 2. Architecture

Clean separation of concerns — each class does one thing and is independently
unit-tested (`test_ladder.py`):

| Component | Responsibility |
|---|---|
| `PerformanceTracker` | Rolling window of the last *N* outcomes → `win_rate`, signed `streak` |
| `StakeManager` | Pure policy: `win_rate` → tier multiplier (stateless) |
| `RiskGuardrails` | Always-on safety overrides (drawdown / streak / vol / cooldown / ceiling) |
| `StakeLadder` | Orchestrator + JSON persistence — the public API |

### Public API (the three hooks)

```python
ladder = StakeLadder()                       # loads persisted state if present

decision = ladder.get_stake(base_stake)      # BEFORE every trade (deterministic)
place_order(size=decision.stake)

ladder.on_trade_result(won=True, pnl=2.10)   # AFTER settlement
# ladder.update_performance(...) is an alias of on_trade_result
```

`get_stake()` returns a `StakeDecision` carrying everything the spec requires us
to log: current win rate, selected tier, stake size, and the reason for the
decision.

---

## 3. Ladder logic

### Rolling performance window
- Scores the **last N = 30** trades (configurable 20–50 via `LADDER_WINDOW`).
- Below `LADDER_MIN_TRADES` (default 10) the ladder stays at **baseline (1×)** —
  a small early sample isn't trustworthy enough to size up on.
- Tracks a **signed streak**: `+k` = k consecutive wins, `-k` = k consecutive
  losses.

### Stake scaling tiers (core ladder)

`stake = base_stake × multiplier`. The first tier whose threshold the win rate
clears wins:

| Tier | Win rate | Multiplier | Intent |
|---|---|---|---|
| T1 Conservative | `< 50%` | **0.5×** | losing edge — shrink |
| T2 Baseline | `50%–55%` | **1.0×** | neutral |
| T3 Momentum | `55%–60%` | **1.25×** | edge confirming |
| T4 Strong | `60%–65%` | **1.5×** | strong edge |
| T5 Aggressive | `≥ 65%` | **2.0×** | press the advantage |

### Safety / drawdown controls (always on, in priority order)

1. **Daily drawdown** — if `daily_pnl ≤ -MAX_DAILY_LOSS_DOLLARS`, revert to
   baseline (`LADDER_DRAWDOWN_ACTION=revert`, default) or pause sizing to `$0`
   (`=pause`). Highest priority — outranks every tier.
2. **Losing streak** — `≥ 4` consecutive losses (`LADDER_STREAK_DEMOTE_AT`)
   demotes the stake exactly **one rung** down the ladder.
3. **Volatility spike** — an externally-fed `set_vol_spike(True)` flag caps the
   stake at baseline.
4. **Ceiling** — the multiplier is hard-capped at **2.0×**
   (`LADDER_MAX_MULT`). Stake can *never* exceed 2× base.

### Cooling logic (anti-chase)

After **every loss** the ladder arms a cooldown lasting the longer of
`LADDER_COOLDOWN_SECS` (default 300s) **and** `LADDER_COOLDOWN_CYCLES` (default
1) trade cycles. While the cooldown is active the stake is capped at baseline.
Because the cooldown persists into the next trade, it also blocks an immediate
size-up on the first post-loss *win* — exactly the "don't jump back up the
moment you flip loss → win" protection the spec asks for.

---

## 4. Integration with the bot

The overlay sits on top of the existing fractional-Kelly sizer. In
`bot.kelly_bet()`:

```python
base_bet = round(min(full_kelly * kf * balance, TRADE_SIZE_CAP,
                     balance * MAX_BET_FRACTION), 2)

if stake_ladder is not None:
    ceiling  = min(stake_ladder.cfg.max_multiplier * TRADE_SIZE_CAP,
                   balance * MAX_BET_FRACTION)
    decision = stake_ladder.get_stake(base_bet, max_stake=ceiling)
    return decision.stake
return base_bet
```

The Kelly stake is the `base_stake`; the ladder multiplies it. The final stake
is re-clamped so it can never break the bot's existing risk limits — `2 ×
TRADE_SIZE_CAP` and the balance-fraction cap both still bind. Settlements feed
the ladder from `resolve_open_orders()` via `ladder_record(won, pnl)` (paper,
live, and pre-restart branches).

**Opt-in by design.** `LADDER_ENABLED` defaults to **`false`**, so live sizing
is byte-for-byte unchanged until you deliberately switch the overlay on. This is
real money — the new sizing behaviour does not go live on its own.

### Clawback sizing: recovery → probation ramp (v9.6.0)

The base stake the ladder multiplies is *itself* mode-driven (`active_trade_size()`):

1. **Recovery** (`recovery.active`): after a full-size loss, base = `RECOVERY_TRADE_SIZE`
   until the balance climbs back to the pre-loss target.
2. **Probation ramp** (`probation.active`): when recovery clears, the base does
   **not** snap back to full size. It re-enters at the recovery floor and climbs a
   ladder of sub-full sizes (default `$100 → $250 → $500`), advancing **one rung**
   on a short win streak *or* a rolling win-rate threshold (whichever fires first)
   and stepping **one rung down** on any loss. Reaching full size graduates back to
   normal.
3. **Normal**: base = `NORMAL_TRADE_SIZE`.

While clawing back (recovery **or** probation), the overlay ceiling is the active
base itself — the ladder may size **down** on a cold streak but can **never size
up**. This closes the 2026-06-29 leak where a `$100` recovery base was scaled to
`$200` by a 2× tier earned at small stakes. The graduation step re-arms the usual
`RECOVERY_LADDER_PAUSE_TRADES` ladder cooldown so the overlay also can't 2× the
instant full size returns. The ramp is purely a sizing overlay — trading never
wedges; the worst case is grinding at the reduced floor.

---

## 5. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `LADDER_ENABLED` | `false` | Master switch for the overlay |
| `LADDER_WINDOW` | `30` | Rolling window size (20–50) |
| `LADDER_MIN_TRADES` | `10` | Warm-up trades before sizing up |
| `LADDER_MAX_MULT` | `2.0` | Absolute multiplier ceiling |
| `MAX_DAILY_LOSS_DOLLARS` | `15.0` | Drawdown override trigger (shared with bot) |
| `LADDER_DRAWDOWN_ACTION` | `revert` | `revert` → 1× base, or `pause` → $0 |
| `LADDER_STREAK_DEMOTE_AT` | `4` | Losing streak length that demotes one tier |
| `LADDER_VOL_CAP_AT_BASE` | `true` | Vol-spike flag caps at baseline |
| `LADDER_COOLDOWN_SECS` | `300` | Post-loss cooldown (seconds) |
| `LADDER_COOLDOWN_CYCLES` | `1` | Post-loss cooldown (trade cycles) |
| `LADDER_STATE_PATH` | `ladder_state.json` | Persistence file |
| `LADDER_PERSIST` | `true` | Toggle JSON persistence |

### Recovery / probation-ramp sizing (v9.5.0–v9.6.0)

| Env var | Default | Meaning |
|---|---|---|
| `NORMAL_TRADE_SIZE` | `TRADE_SIZE_DOLLARS` | Full-size base stake |
| `RECOVERY_TRADE_SIZE` | `100` | Reduced base while clawing back a full-size loss |
| `RECOVERY_LADDER_PAUSE_TRADES` | `5` | Hold ladder multiplier at 1× for N trades after full size returns |
| `PROBATION_RAMP_ENABLED` | `true` | Graduated re-entry after recovery (false = old snap-back to full) |
| `PROBATION_WIN_STREAK` | `2` | Consecutive wins at a rung that advance one step |
| `PROBATION_WIN_RATE_MIN` | `0.60` | ...or rolling win rate that advances one step (either fires) |
| `PROBATION_WINRATE_MIN_TRADES` | `4` | Min settled ramp trades before the win-rate path can fire |
| `PROBATION_RUNGS` | _(auto)_ | Explicit sub-full base sizes, e.g. `100,250` (else `[floor, full/2]`) |
| `PROBATION_STATE_PATH` | `probation_state.json` | Persistence file |
| `PROBATION_PERSIST` | `true` | Toggle JSON persistence |

---

## 6. Example simulation

`python ladder.py` runs a deterministic 20-trade tape — warm-up, a hot run that
ladders to aggressive, then a 4-loss streak that demotes and triggers cooldown
(base stake `$5`):

```
 #  stake  mult tier                     WR reason
------------------------------------------------------------------------------
 1 $ 5.00  1.00 T2-BASELINE(warmup)    0.0% warmup 0/10 trades
 2 $ 5.00  1.00 T2-BASELINE(warmup)  100.0% warmup 1/10 trades
 ...
10 $ 5.00  1.00 T2-BASELINE(warmup)   77.8% warmup 9/10 trades
11 $ 5.00  1.00 T5-AGGRESSIVE         80.0% cooldown cap→base
12 $10.00  2.00 T5-AGGRESSIVE         81.8% tier T5-AGGRESSIVE clean
13 $10.00  2.00 T5-AGGRESSIVE         83.3% tier T5-AGGRESSIVE clean
14 $10.00  2.00 T5-AGGRESSIVE         84.6% tier T5-AGGRESSIVE clean
15 $10.00  2.00 T5-AGGRESSIVE         85.7% tier T5-AGGRESSIVE clean
16 $ 5.00  1.00 T5-AGGRESSIVE         80.0% cooldown cap→base
17 $ 5.00  1.00 T5-AGGRESSIVE         75.0% cooldown cap→base
18 $ 5.00  1.00 T5-AGGRESSIVE         70.6% cooldown cap→base
19 $ 5.00  1.00 T5-AGGRESSIVE         66.7% streak 4≥4 demote; cooldown cap→base
20 $ 5.00  1.00 T5-AGGRESSIVE         68.4% cooldown cap→base
```

Reading it:
- **1–10**: warm-up — baseline regardless of the noisy early win rate.
- **11**: window is warm and WR is high, but the loss back at trade 7 still has
  the cooldown active → held at base. Anti-chase working.
- **12–15**: cooldown clear, WR ≥ 65% → full **2× aggressive**, `$10`.
- **16–19**: a 4-loss run. Each loss re-arms cooldown (caps at base) and by
  trade 19 the streak demote also fires. Stake stays defensive at `$5` even
  though the rolling WR is still elevated — the guardrails lead.

---

## 7. Edge-case handling

**Losing streaks.** Two independent brakes: the cooldown caps at baseline after
*every* loss, and a `≥4` streak demotes a full rung. Combined with the bot's own
`MAX_CONSEC_LOSSES` pause, a cold run de-risks fast instead of doubling down.

**Chop / mean-reverting markets.** Alternating W/L keeps the win rate hovering
near 50% (baseline) while the cooldown — re-armed on each loss — repeatedly
denies size-ups. The ladder settles at ~1× in chop rather than whipsawing
between aggressive and conservative.

**Overfitting to a small sample.** The `min_trades` warm-up gate refuses to size
up on a handful of trades, and the bounded window means one lucky run ages out
after N trades rather than permanently inflating the stake. Tiers are coarse
(5 buckets), so the ladder reacts to genuine shifts, not single-trade noise.

**Variance overexposure.** The 2× ceiling, the balance-fraction cap, and the
daily-drawdown override together bound the worst case. Even a perfect win rate
cannot push the stake past `2 × TRADE_SIZE_CAP`, and a bad day reverts (or
pauses) sizing regardless of what the rolling window says.

**Restarts.** State (window, streak, daily PnL, cooldown) persists to
`ladder_state.json` and reloads on boot. A stale `daily_pnl` from a previous UTC
day is zeroed on load so yesterday's drawdown can't suppress today's sizing.

**Corrupt / missing state.** A malformed state file is ignored (the ladder
starts cold) rather than crashing the bot.

---

## 8. Tests

```bash
pytest test_ladder.py -v     # 38 unit tests: tracker, tiers, guardrails, cooldown, persistence
pytest test_bot.py -v        # includes TestLadderIntegration (overlay respects caps)
```
