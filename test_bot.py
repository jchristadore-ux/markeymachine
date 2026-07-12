"""
test_bot.py — Pytest suite for MarkeyMachine v9.3.1

Covers:
  P0: All risk controls
  P1: Signal math (edge, Kelly, momentum, confidence, regime)
  P2: OB analysis, stale cancel, ob trend
  P3: Wilson CI, performance guard, Bayesian prior
  v9.3.0: doctrine Layer-7 AGREE gate, NEUTRAL confidence weight, restored thresholds

Usage:
  pip install pytest
  pytest test_bot.py -v
"""

import os
import time
import math

os.environ.setdefault("KALSHI_API_KEY_ID", "test-key-id-00000000")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PEM", "")
os.environ.setdefault("DEMO_MODE", "true")

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

_test_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_test_pem = _test_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")
os.environ["KALSHI_PRIVATE_KEY_PEM"] = _test_pem

import pytest
import bot
from bot import Regime, SessionState
from ladder import StakeLadder, LadderConfig


class _FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


class TestLadderIntegration:
    """The laddering overlay scales the Kelly stake but never breaks the bot's
    existing caps. It is opt-in: bot.stake_ladder is None unless LADDER_ENABLED."""

    def teardown_method(self):
        bot.stake_ladder = None

    def test_disabled_by_default_leaves_kelly_unchanged(self):
        bot.stake_ladder = None
        bet = bot.kelly_bet(0.65, 50, 100.0)
        assert bet > 0  # plain Kelly path, no overlay

    def test_overlay_scales_up_on_hot_record(self):
        clk = _FakeClock()
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=10,
                                           cooldown_secs=0), clock=clk)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        bot.stake_ladder = None
        plain = bot.kelly_bet(0.65, 50, 1000.0)
        bot.stake_ladder = lad
        laddered = bot.kelly_bet(0.65, 50, 1000.0)
        # 2x tier sizes up vs the un-laddered Kelly stake (subject to caps).
        assert laddered >= plain

    def test_overlay_never_exceeds_2x_cap(self):
        clk = _FakeClock()
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=10,
                                           max_multiplier=2.0, cooldown_secs=0),
                          clock=clk)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        bot.stake_ladder = lad
        # Large balance so the balance-fraction cap is not the binding limit.
        bet = bot.kelly_bet(0.65, 50, 100_000.0)
        assert bet <= 2.0 * bot.active_trade_size() + 1e-9


class TestPostBootSettlementGate:
    """v9.0.8: the unmatched-settlement branch must only count records settled
    at/after boot. /portfolio/settlements ignores created_since and returns
    account-wide history; counting it all deadlocked the Wilson perf guard."""

    def setup_method(self):
        bot._session_start_ts = "2026-06-11T23:00:00Z"

    def teardown_method(self):
        bot._session_start_ts = ""

    def test_account_history_before_boot_excluded(self):
        assert bot._is_post_boot({"settled_time": "2026-06-09T15:15:00Z"}) is False

    def test_in_flight_settled_after_boot_counted(self):
        assert bot._is_post_boot({"settled_time": "2026-06-11T23:45:00Z"}) is True

    def test_created_time_fallback(self):
        assert bot._is_post_boot({"created_time": "2026-06-11T23:45:00Z"}) is True

    def test_missing_timestamp_excluded(self):
        assert bot._is_post_boot({}) is False

    def test_unparseable_timestamp_excluded(self):
        assert bot._is_post_boot({"settled_time": "garbage"}) is False

    def test_no_boot_ts_excluded(self):
        bot._session_start_ts = ""
        assert bot._is_post_boot({"settled_time": "2026-06-11T23:45:00Z"}) is False


# ═════════════════════════════════════════════════════════════════════════════
# v9.3.0: DOCTRINE LAYER 7 — AGREE-REQUIRED MOMENTUM GATE
# ═════════════════════════════════════════════════════════════════════════════

class TestMomentumGate:
    """The single fix for the 2026-06-20→22 bleed: NEUTRAL momentum must NOT
    trade. All 6 bleeding trades fired on BTC=NEUTRAL because no gate existed."""

    def test_agree_passes_when_required(self, monkeypatch):
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", True)
        assert bot.momentum_gate_ok("AGREE") is True

    def test_neutral_blocked_when_required(self, monkeypatch):
        # This is the bleed. NEUTRAL must be rejected by default.
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", True)
        assert bot.momentum_gate_ok("NEUTRAL") is False

    def test_conflict_blocked_when_required(self, monkeypatch):
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", True)
        assert bot.momentum_gate_ok("CONFLICT") is False

    def test_gate_off_allows_neutral(self, monkeypatch):
        # Escape hatch for the deliberate unconfirmed-OB experiment only.
        monkeypatch.setattr(bot, "REQUIRE_AGREE_MOMENTUM", False)
        assert bot.momentum_gate_ok("NEUTRAL") is True

    def test_default_is_on(self):
        # The doctrine default must be ON so an unset env var is safe.
        assert bot.REQUIRE_AGREE_MOMENTUM is True


class TestConfidenceNeutralWeight:
    """v9.3.0: NEUTRAL momentum restored to 2 pts (doctrine Layer 8). The
    v9.0.6 bump to 8 pts lifted marginal flat-BTC setups over the 65 bar."""

    def _ob(self, imbalance=0.71, depth=34000.0, eff_thresh=0.66):
        return {"imbalance": imbalance, "total_depth": depth,
                "eff_thresh": eff_thresh}

    def test_neutral_scores_less_than_agree(self):
        common = dict(ob=self._ob(), regime=Regime.TRENDING_DOWN, r_squared=0.82,
                      win_prob=0.72, mins_remaining=14.0, session_score=60)
        neutral = bot.compute_confidence(momentum_verdict="NEUTRAL", **common)
        agree   = bot.compute_confidence(momentum_verdict="AGREE", **common)
        assert agree - neutral == pytest.approx(13.0, abs=0.01)  # 15 vs 2

    def test_06_20_0830_trade_now_blocked(self, monkeypatch):
        """Reproduce the 06-20 08:30 trade that scored Conf=65 on mom=8.0.
        With NEUTRAL=2 it must fall below the restored MIN_CONFIDENCE=65."""
        monkeypatch.setattr(bot, "MIN_CONFIDENCE", 65)
        # Approximate the logged components: imb≈4.5 depth≈10.9 regime≈23.8
        # prob≈7.9 time≈10.0 → with NEUTRAL=8 these summed to 65.
        conf = bot.compute_confidence(
            ob=self._ob(imbalance=0.673, depth=31053.0, eff_thresh=0.66),
            regime=Regime.TRENDING_DOWN, r_squared=0.87,
            momentum_verdict="NEUTRAL", win_prob=0.721,
            mins_remaining=14.1, session_score=60)
        assert conf < 65


class TestRestoredThresholds:
    """v9.3.0: the four drifted defaults are back at doctrine values."""

    def test_ob_imbalance_threshold(self):
        assert bot.OB_IMBALANCE_THRESH == 0.70

    def test_r2_trend_threshold(self):
        assert bot.R2_TREND_THRESHOLD == 0.65

    def test_min_confidence(self):
        assert bot.MIN_CONFIDENCE == 65

    def test_yes_breakeven_price(self):
        assert bot.YES_BREAKEVEN_PRICE == 67

    def test_neutral_drag_restored(self):
        assert bot.NEUTRAL_ACCURACY_DRAG == 0.02


# ═════════════════════════════════════════════════════════════════════════════
# P0: RISK CONTROLS
# ═════════════════════════════════════════════════════════════════════════════

class TestDailyLossCheck:
    """v9.4.0: the % and $ daily-loss governors were removed by owner directive.
    daily_loss_check() now enforces ONLY the 40% catastrophic session stop; the
    consecutive-loss streak pause (streak_check) is the active auto-hold."""

    def setup_method(self):
        bot._session_halted = False

    def test_large_paper_loss_no_longer_halts(self, monkeypatch):
        # A deep daily drawdown must NOT halt now that the daily caps are gone.
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -5000.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(1000.0) is True
        assert bot._session_halted is False

    def test_large_realized_loss_no_longer_halts(self, monkeypatch):
        # Same in LIVE mode: realized drawdown alone no longer trips a breaker.
        monkeypatch.setattr(bot, "DEMO_MODE", False)
        monkeypatch.setattr(bot, "live_daily_realized", -5000.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(1000.0) is True
        assert bot._session_halted is False

    def test_session_stop_triggers(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        assert bot.daily_loss_check(10.0) is False

    def test_session_stop_ok_when_above(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        assert bot.daily_loss_check(20.0) is True

    def test_halted_flag_blocks(self, monkeypatch):
        monkeypatch.setattr(bot, "_session_halted", True)
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(50.0) is False

    def test_session_stop_halt_is_permanent(self, monkeypatch):
        """Balance recovery above session_stop_threshold must not clear the halt."""
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        # Trigger the catastrophic session stop at low balance
        assert bot.daily_loss_check(10.0) is False
        assert bot._session_halted is True
        # Even with high balance, stays halted
        assert bot.daily_loss_check(50.0) is False


class TestSpreadCheck:
    def test_normal(self):
        assert bot.spread_check(48, 52) is True

    def test_one_cent(self):
        assert bot.spread_check(49, 50) is True

    def test_zero(self):
        assert bot.spread_check(50, 50) is False

    def test_crossed(self):
        assert bot.spread_check(52, 48) is False


class TestExpiryGuard:
    def test_near_certain_high(self):
        assert bot.expiry_guard(90) is False

    def test_near_certain_low(self):
        assert bot.expiry_guard(10) is False

    def test_boundary_high_blocked(self):
        assert bot.expiry_guard(86) is False

    def test_boundary_high_allowed(self):
        assert bot.expiry_guard(85) is True

    def test_boundary_low_blocked(self):
        assert bot.expiry_guard(14) is False

    def test_boundary_low_allowed(self):
        assert bot.expiry_guard(15) is True

    def test_mid(self):
        assert bot.expiry_guard(50) is True


class TestCooldownCheck:
    def test_not_passed(self, monkeypatch):
        monkeypatch.setattr(bot, "last_trade_ts", time.time())
        assert bot.cooldown_check() is False

    def test_passed(self, monkeypatch):
        monkeypatch.setattr(bot, "last_trade_ts", time.time() - 9999)
        assert bot.cooldown_check() is True


class TestStreakCheck:
    def setup_method(self):
        bot.consecutive_losses = 0
        bot.streak_pause_until = 0.0

    def test_no_losses_ok(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 0
        assert bot.streak_check() is True

    def test_below_threshold_ok(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 1
        assert bot.streak_check() is True

    def test_at_threshold_in_pause(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 2
        bot.streak_pause_until = time.time() + 9999
        assert bot.streak_check() is False

    def test_at_threshold_pause_expired(self, monkeypatch):
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 2)
        bot.consecutive_losses = 2
        bot.streak_pause_until = time.time() - 1
        result = bot.streak_check()
        assert result is True
        assert bot.consecutive_losses == 0

    def test_three_loss_threshold_is_the_only_auto_hold(self, monkeypatch):
        """v9.4.0: at MAX_CONSEC_LOSSES=3, two losses keep trading; three pause."""
        monkeypatch.setattr(bot, "MAX_CONSEC_LOSSES", 3)
        bot.streak_pause_until = time.time() + 9999
        bot.consecutive_losses = 2
        assert bot.streak_check() is True   # 2 losses → still trading
        bot.consecutive_losses = 3
        assert bot.streak_check() is False  # 3 losses → paused


# ═════════════════════════════════════════════════════════════════════════════
# P1: SIGNAL MATH
# ═════════════════════════════════════════════════════════════════════════════

class TestCalcEdge:
    def test_positive_edge(self):
        edge = bot.calc_edge(0.70, 50)
        assert abs(edge - 0.20) < 0.001

    def test_zero_edge(self):
        edge = bot.calc_edge(0.50, 50)
        assert abs(edge) < 0.001

    def test_negative_edge(self):
        assert bot.calc_edge(0.30, 50) < 0

    def test_boundary_zero_price(self):
        assert bot.calc_edge(0.70, 0) == 0.0

    def test_boundary_100_price(self):
        assert bot.calc_edge(0.70, 100) == 0.0

    def test_cheap_contract(self):
        edge = bot.calc_edge(0.40, 20)
        assert abs(edge - 0.20) < 0.001


class TestKellyBet:
    def setup_method(self):
        # Sizing is now mode-derived; default every test to NORMAL mode.
        bot.recovery.active         = False
        bot.recovery.target_balance = 0.0

    def test_positive_edge_returns_bet(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        bet = bot.kelly_bet(0.70, 50, 25.0)
        assert bet > 0
        assert bet <= 5.0

    def test_no_edge_returns_zero(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        bet = bot.kelly_bet(0.30, 50, 25.0)
        assert bet == 0.0

    def test_capped_at_trade_size(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 2.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.50)
        bet = bot.kelly_bet(0.90, 40, 100.0)
        assert bet <= 2.0

    def test_flat_500_on_large_bankroll(self, monkeypatch):
        """v9.4.1: flat $500 stake on any positive-edge trade at a high balance."""
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        assert bot.kelly_bet(0.90, 40, 5000.0) == 500.0

    def test_flat_500_fires_regardless_of_balance(self, monkeypatch):
        """v9.4.1: a modest-edge trade on a small (but ≥$500) balance still
        stakes the full $500 — no Kelly/balance down-scaling."""
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        assert bot.kelly_bet(0.70, 50, 600.0) == 500.0

    def test_clamped_to_cash_when_balance_below_stake(self, monkeypatch):
        """v9.4.1: the only clamp is cash on hand — below $500 the bot goes all-in."""
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        assert bot.kelly_bet(0.70, 50, 300.0) == 300.0

    def test_high_stake_gated_below_min_balance(self, monkeypatch):
        """v9.8.0: a $1000 ceiling is capped to $500 until equity ≥ $5000, then
        the full stake fires."""
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 1000.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        monkeypatch.setattr(bot, "HIGH_STAKE_GATE_SIZE", 500.0)
        monkeypatch.setattr(bot, "HIGH_STAKE_MIN_BALANCE", 5000.0)
        bot.probation.active = False
        assert bot.kelly_bet(0.90, 40, 4000.0) == 500.0    # gated
        assert bot.kelly_bet(0.90, 40, 6000.0) == 1000.0   # unlocked

    def test_recovery_mode_uses_recovery_size(self, monkeypatch):
        """v9.5.0: while recovery is active, sizing derives from RECOVERY_TRADE_SIZE."""
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        bot.recovery.active = True
        assert bot.kelly_bet(0.90, 40, 5000.0) == 100.0

    def test_boundary_prices(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        assert bot.kelly_bet(0.70, 0, 25.0) == 0.0
        assert bot.kelly_bet(0.70, 100, 25.0) == 0.0

    def test_bet_fraction_no_longer_caps(self, monkeypatch):
        """v9.4.1: MAX_BET_FRACTION is dead config — flat sizing ignores it.
        A small fraction must NOT shrink the stake below NORMAL_TRADE_SIZE."""
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.04)  # would have capped at $40
        assert bot.kelly_bet(0.90, 40, 1_000.0) == 500.0


class TestComputeMomentum:
    def setup_method(self):
        bot.btc_prices.clear()

    def test_insufficient_data(self, monkeypatch):
        # v9.3.2: with the default lookback (6) a single price is far short of
        # the MOMENTUM_LOOKBACK+1 samples needed.
        bot.btc_prices.append(50000)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"

    def test_agree_yes_btc_up(self, monkeypatch):
        # 4-price series exercises the 3-interval window; pin lookback to 3.
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 3)
        for p in [50000, 50050, 50100, 50200, 50300]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "AGREE"
        assert adj > 0

    def test_conflict_yes_btc_down(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 3)
        for p in [50000, 49900, 49800, 49700, 49500]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "CONFLICT"

    def test_neutral_choppy(self, monkeypatch):
        # v9.3.3: NEUTRAL now means genuinely directionless BTC — BOTH low
        # regression R² AND sub-threshold magnitude. A symmetric zigzag (net ~0%,
        # slope ~0, R²≈0) is the chop the doctrine rejects.
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_R2_MIN", 0.55)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        for p in [50000, 50060, 50000, 49940, 50000, 50060, 50000]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"

    def test_agree_no_btc_down(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 3)
        for p in [50000, 49900, 49800, 49700, 49500]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("NO")
        assert verdict == "AGREE"

    def test_neutral_adj_is_negative(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_R2_MIN", 0.55)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        monkeypatch.setattr(bot, "NEUTRAL_ACCURACY_DRAG", 0.02)
        for p in [50000, 50060, 50000, 49940, 50000, 50060, 50000]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"
        assert adj == -0.02

    def test_lookback_default_needs_more_samples(self, monkeypatch):
        # At the default lookback (6) a 5-price series is one sample short, so
        # momentum cannot yet be measured.
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        for p in [50000, 50050, 50100, 50200, 50300]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"

    def test_gentle_consistent_trend_agrees_via_r2(self, monkeypatch):
        # v9.3.3 regression for the 2026-06-24 "zero trades" bug. A clean, gentle
        # uptrend whose magnitude stays BELOW MOMENTUM_THRESH_PCT but is highly
        # consistent (R²≈1) used to read NEUTRAL and block every trade. The R²
        # path now recognizes it as a real trend and AGREES.
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_R2_MIN", 0.55)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        # +8/step over 6 steps → +0.096% net (< 0.15% thresh), perfectly linear.
        for p in [50000 + 8 * i for i in range(7)]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "AGREE"
        assert adj > 0

    def test_gentle_consistent_trend_conflicts_against_ob(self, monkeypatch):
        # Same gentle-but-consistent trend, opposite OB side → CONFLICT (the
        # slope direction disagrees with the order book).
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_R2_MIN", 0.55)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        for p in [50000 + 8 * i for i in range(7)]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("NO")
        assert verdict == "CONFLICT"

    def test_r2_path_disabled_restores_magnitude(self, monkeypatch):
        # Setting MOMENTUM_R2_MIN=2.0 disables the R² path: the same gentle trend
        # falls back to pure magnitude and (being sub-threshold) reads NEUTRAL,
        # i.e. the pre-9.3.3 behavior is recoverable via env.
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_R2_MIN", 2.0)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        for p in [50000 + 8 * i for i in range(7)]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"


class TestRegimeAgreement:
    """The direction gate that blocks betting against the measured trend.

    Backed by 2026-06-19 logs: aligned trade won, both conflicted trades lost.
    """

    def test_up_favors_yes(self):
        assert bot.regime_direction(bot.Regime.TRENDING_UP) == "YES"

    def test_down_favors_no(self):
        assert bot.regime_direction(bot.Regime.TRENDING_DOWN) == "NO"

    def test_ranging_has_no_favored_side(self):
        assert bot.regime_direction(bot.Regime.RANGING) is None

    def test_yes_in_uptrend_agrees(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_UP, "YES") is True

    def test_no_in_uptrend_conflicts(self):
        # Trade 2 (2026-06-19): NO bet in TRENDING_UP — lost.
        assert bot.regime_agrees(bot.Regime.TRENDING_UP, "NO") is False

    def test_yes_in_downtrend_conflicts(self):
        # Trade 3 (2026-06-19): YES bet in TRENDING_DOWN — lost.
        assert bot.regime_agrees(bot.Regime.TRENDING_DOWN, "YES") is False

    def test_no_in_downtrend_agrees(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_DOWN, "NO") is True

    def test_case_insensitive(self):
        assert bot.regime_agrees(bot.Regime.TRENDING_UP, "yes") is True


class TestBayesianWinProbImbalance:
    """v9.2.0: order-book strength must move the win probability."""

    def _ob(self, imbalance, depth=2500.0, eff_thresh=0.60):
        return {"imbalance": imbalance, "total_depth": depth,
                "eff_thresh": eff_thresh}

    def test_stronger_book_raises_win_prob(self, monkeypatch):
        monkeypatch.setattr(bot, "_live_prior", 0.635)
        weak = bot.bayesian_win_prob(
            self._ob(0.61), "NEUTRAL", 0.0, bot.Regime.TRENDING_UP, 0.78, 0.05)
        strong = bot.bayesian_win_prob(
            self._ob(0.90), "NEUTRAL", 0.0, bot.Regime.TRENDING_UP, 0.78, 0.05)
        assert strong > weak

    def test_imbalance_contribution_is_capped(self, monkeypatch):
        monkeypatch.setattr(bot, "_live_prior", 0.635)
        # An extreme imbalance must not blow past the 0.92 hard ceiling.
        wp = bot.bayesian_win_prob(
            self._ob(0.999), "AGREE", 0.045, bot.Regime.TRENDING_UP, 0.95, 0.0)
        assert wp <= 0.92


class TestSessionDayRollover:
    """v9.2.0: the daily halt is paused-for-the-day, not permanent."""

    def setup_method(self):
        bot._session_day      = "2026-06-19"
        bot._session_halted   = True
        bot.session_start_balance  = 2000.0
        bot.session_stop_threshold = 800.0
        bot.daily_pnl         = -135.0
        bot.paper_daily_pnl   = -135.0
        bot.consecutive_losses = 2
        bot.session_state     = SessionState.RECOVERY
        bot.session_traded_tickers.add("KXBTC15M-OLD")
        bot.recovery.active = False
        bot.probation.cancel()

    def teardown_method(self):
        bot.recovery.active = False
        bot.probation.cancel()

    def _enable_ramp(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)
        monkeypatch.setattr(bot, "PROBATION_RAMP_ENABLED", True)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "")

    def test_same_day_is_noop(self, monkeypatch):
        monkeypatch.setattr(bot, "_session_day",
                            bot.datetime.now(bot.timezone.utc).strftime("%Y-%m-%d"))
        bot._session_halted = True
        assert bot.maybe_roll_session_day(1800.0) is False
        assert bot._session_halted is True

    def test_new_day_clears_halt_and_rebaselines(self):
        # setup_method left _session_day at a stale date → rollover fires.
        rolled = bot.maybe_roll_session_day(1850.0)
        assert rolled is True
        assert bot._session_halted is False
        assert bot.session_start_balance == 1850.0
        assert bot.daily_pnl == 0.0
        assert bot.consecutive_losses == 0
        assert bot.session_state == SessionState.ACTIVE
        assert "KXBTC15M-OLD" not in bot.session_traded_tickers

    def test_new_day_rearms_slow_roll_ramp_from_floor(self, monkeypatch):
        # v9.7.0: a fresh day re-enters the $100 → $250 → $500 ramp at the floor
        # so the first trade of the day is small, not full size.
        self._enable_ramp(monkeypatch)
        assert bot.maybe_roll_session_day(1850.0) is True
        assert bot.probation.active is True
        assert bot.probation.current_size() == bot.RECOVERY_TRADE_SIZE   # $100

    def test_new_day_resets_halfclimbed_ramp(self, monkeypatch):
        # A ramp left at a higher rung yesterday drops back to the floor today.
        self._enable_ramp(monkeypatch)
        bot.probation.start([100.0, 250.0], 500.0)
        bot.probation.record_result(True); bot.probation.record_result(True)
        assert bot.probation.current_size() == 250.0                    # climbed
        assert bot.maybe_roll_session_day(1850.0) is True
        assert bot.probation.current_size() == 100.0                    # reset

    def test_new_day_skips_rearm_during_recovery(self, monkeypatch):
        # Recovery is the deeper claw-back tier and must take priority.
        self._enable_ramp(monkeypatch)
        bot.recovery.active = True
        assert bot.maybe_roll_session_day(1850.0) is True
        assert bot.probation.active is False

    def test_new_day_no_ramp_when_disabled(self, monkeypatch):
        self._enable_ramp(monkeypatch)
        monkeypatch.setattr(bot, "PROBATION_RAMP_ENABLED", False)
        assert bot.maybe_roll_session_day(1850.0) is True
        assert bot.probation.active is False                            # stays $500


class TestWilsonCI:
    def test_zero_trades(self):
        pct, lo, hi = bot.wilson_confidence(0, 0)
        assert pct == 0.0

    def test_all_wins(self):
        pct, lo, hi = bot.wilson_confidence(10, 10)
        assert pct == 100.0
        assert lo > 50.0

    def test_all_losses(self):
        pct, lo, hi = bot.wilson_confidence(0, 10)
        assert pct == 0.0
        assert hi < 50.0

    def test_fifty_fifty(self):
        pct, lo, hi = bot.wilson_confidence(50, 100)
        assert abs(pct - 50.0) < 0.1
        assert lo < 50.0
        assert hi > 50.0

    def test_large_sample_narrow_ci(self):
        pct, lo, hi = bot.wilson_confidence(70, 100)
        assert hi - lo < 20


class TestWilsonLowerBound:
    def test_small_sample_returns_zero(self):
        assert bot.wilson_lower_bound(5, 9) == 0.0

    def test_good_win_rate(self):
        wlb = bot.wilson_lower_bound(15, 20)
        assert wlb > 0.50

    def test_bad_win_rate(self):
        wlb = bot.wilson_lower_bound(5, 20)
        assert wlb < 0.50


# ═════════════════════════════════════════════════════════════════════════════
# P1: REGIME DETECTION
# ═════════════════════════════════════════════════════════════════════════════

class TestComputeRegime:
    def setup_method(self):
        bot.btc_prices.clear()
        bot.btc_returns.clear()

    def test_insufficient_data_returns_unknown(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        for p in range(5):
            bot.btc_prices.append(50000 + p * 10)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.UNKNOWN

    def test_strong_uptrend(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 1.0)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        prices = [50000 + i * 100 for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        for i in range(1, len(prices)):
            bot.btc_returns.append((prices[i] - prices[i-1]) / prices[i-1] * 100)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.TRENDING_UP
        assert r2 > 0.70

    def test_strong_downtrend(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 1.0)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        prices = [50000 - i * 100 for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        for i in range(1, len(prices)):
            bot.btc_returns.append((prices[i] - prices[i-1]) / prices[i-1] * 100)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.TRENDING_DOWN

    def test_ranging(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 1.0)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        import math
        prices = [50000 + int(math.sin(i) * 200) for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        for i in range(1, len(prices)):
            r = (prices[i] - prices[i-1]) / prices[i-1] * 100
            bot.btc_returns.append(r)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.RANGING

    def test_high_vol(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_PRICES_FOR_REGIME", 10)
        monkeypatch.setattr(bot, "R2_TREND_THRESHOLD", 0.70)
        monkeypatch.setattr(bot, "VOLATILITY_CAP_PCT", 0.10)
        monkeypatch.setattr(bot, "VOL_CIRCUIT_BREAKER", 5.0)
        monkeypatch.setattr(bot, "TREND_LOOKBACK", 12)
        prices = [50000 + i * 100 for i in range(15)]
        for p in prices:
            bot.btc_prices.append(p)
        # Force high returns
        for _ in range(14):
            bot.btc_returns.append(0.50)
        regime, r2, vol = bot.compute_regime()
        assert regime == Regime.HIGH_VOL


# ═════════════════════════════════════════════════════════════════════════════
# P2: ORDER BOOK ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

class TestAnalyzeOrderBook:
    def _make_ob(self, yes_levels, no_levels):
        return {"orderbook_fp": {"yes_dollars": yes_levels, "no_dollars": no_levels}}

    def test_strong_yes_signal(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.48, 20], [0.50, 20]], [[0.50, 10]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["direction"] == "YES"
        assert result["imbalance"] >= 0.70

    def test_strong_no_signal(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 5]], [[0.48, 20], [0.50, 20]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["direction"] == "NO"

    def test_balanced_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 15]], [[0.50, 15]])
        result = bot.analyze_order_book(ob, 50)
        assert result is None

    def test_thin_book_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 50.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 2]], [[0.50, 1]])
        result = bot.analyze_order_book(ob, 50)
        assert result is None

    def test_ghost_ob_returns_none(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        # YES imbalance but NO side has zero levels
        ob = self._make_ob([[0.48, 80], [0.50, 20]], [])
        result = bot.analyze_order_book(ob, 50)
        assert result is None

    def test_deep_book_lowers_threshold(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 100.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.64)
        # 61% YES would fail at 0.64 threshold but deep books lower to 0.58
        ob = self._make_ob([[0.48, 6100]], [[0.50, 3900]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["direction"] == "YES"
        assert result["eff_thresh"] <= 0.60

    def test_total_depth_correct(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_OB_DEPTH", 5.0)
        monkeypatch.setattr(bot, "OB_IMBALANCE_THRESH", 0.62)
        ob = self._make_ob([[0.50, 30]], [[0.50, 10]])
        result = bot.analyze_order_book(ob, 50)
        assert result is not None
        assert result["total_depth"] == 40.0


class TestCheckObTrend:
    def setup_method(self):
        bot._prev_ob.clear()

    def test_first_obs_allows(self):
        assert bot.check_ob_trend("T1", "YES", 0.70) is True

    def test_building_pressure_allows(self):
        bot._prev_ob["T1"] = ("YES", 0.65, time.time())
        assert bot.check_ob_trend("T1", "YES", 0.72) is True

    def test_fading_pressure_blocks(self):
        bot._prev_ob["T1"] = ("YES", 0.75, time.time())
        assert bot.check_ob_trend("T1", "YES", 0.60) is False

    def test_direction_flip_allows(self):
        # v9.0.1 check_ob_trend only blocks on fading (same direction, >10% drop)
        # Direction flip is NOT blocked by check_ob_trend — it's handled in run_decision
        bot._prev_ob["T1"] = ("YES", 0.70, time.time())
        result = bot.check_ob_trend("T1", "NO", 0.70)
        # Does not block — check_ob_trend only looks at fading same-direction
        assert result is True

    def test_stale_data_allows(self):
        bot._prev_ob["T1"] = ("YES", 0.80, time.time() - 700)
        assert bot.check_ob_trend("T1", "NO", 0.60) is True

    def test_small_fade_allows(self):
        bot._prev_ob["T1"] = ("YES", 0.70, time.time())
        assert bot.check_ob_trend("T1", "YES", 0.66) is True


# ═════════════════════════════════════════════════════════════════════════════
# P2: STALE ORDER CANCELLATION
# ═════════════════════════════════════════════════════════════════════════════

class TestCancelStaleOrders:
    def test_paper_refunds_balance(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 60)
        bot.open_orders.clear()
        bot.active_tickers.clear()
        bot.trade_history.clear()
        bot.paper_balance = 20.0

        bot.open_orders["test-1"] = {
            "ticker": "KXBTC-TEST",
            "cost": 2.50,
            "placed_at": time.time() - 120,
        }
        bot.active_tickers.add("KXBTC-TEST")
        bot.trade_history.append({"order_id": "test-1", "result": "pending"})

        bot.cancel_stale_orders()

        assert "test-1" not in bot.open_orders
        assert bot.paper_balance == 22.50
        assert "KXBTC-TEST" not in bot.active_tickers

    def test_paper_does_not_touch_daily_pnl(self, monkeypatch):
        """Stale cancel is a refund, not a profit/loss."""
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 60)
        bot.open_orders.clear()
        bot.active_tickers.clear()
        bot.trade_history.clear()
        bot.paper_balance = 20.0
        bot.paper_daily_pnl = -3.0

        bot.open_orders["test-2"] = {
            "ticker": "KXBTC-TEST2",
            "cost": 1.00,
            "placed_at": time.time() - 120,
        }
        bot.active_tickers.add("KXBTC-TEST2")

        bot.cancel_stale_orders()

        assert bot.paper_daily_pnl == -3.0  # unchanged

    def test_fresh_order_not_canceled(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "STALE_ORDER_TIMEOUT", 300)
        bot.open_orders.clear()

        bot.open_orders["test-3"] = {
            "ticker": "KXBTC-TEST3",
            "cost": 1.00,
            "placed_at": time.time() - 30,
        }

        bot.cancel_stale_orders()
        assert "test-3" in bot.open_orders


# ═════════════════════════════════════════════════════════════════════════════
# P3: PERFORMANCE GUARD & BAYESIAN PRIOR
# ═════════════════════════════════════════════════════════════════════════════

class TestPerformanceGuard:
    """v9.9.0: the guard de-rates the stake below the Wilson floor instead of
    hard-blocking. A hard block froze the live win record and deadlocked the bot
    (2026-07-03: 11/20, LB 37.2% < 50%, 4,554 warnings, zero trades)."""

    def test_below_min_sample_full_size(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 3)
        monkeypatch.setattr(bot, "live_losses", 5)
        assert bot.performance_guard_multiplier() == 1.0

    def test_good_win_rate_full_size(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 16)
        monkeypatch.setattr(bot, "live_losses", 4)
        assert bot.performance_guard_multiplier() == 1.0

    def test_bad_win_rate_derates_not_blocks(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "PERF_GUARD_FLOOR", 0.50)
        monkeypatch.setattr(bot, "PERF_GUARD_DERATE", 0.25)
        monkeypatch.setattr(bot, "live_wins", 8)
        monkeypatch.setattr(bot, "live_losses", 22)
        # Below the floor: de-rated, but never zero (that would re-freeze).
        assert bot.performance_guard_multiplier() == 0.25

    def test_deadlock_case_still_trades(self, monkeypatch):
        # The exact 2026-07-03 lockout: 11/20 (LB 37.2%) crossed the sample
        # threshold on a winning streak. Under the old guard this returned
        # False forever; it must now de-rate, not block.
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "PERF_GUARD_FLOOR", 0.50)
        monkeypatch.setattr(bot, "PERF_GUARD_DERATE", 0.25)
        monkeypatch.setattr(bot, "live_wins", 11)
        monkeypatch.setattr(bot, "live_losses", 9)
        assert 0.0 < bot.performance_guard_multiplier() < 1.0

    def test_derate_zero_restores_hard_block(self, monkeypatch):
        # PERF_GUARD_DERATE=0.0 opts back into the legacy freeze: a zero stake
        # is skipped by the downstream min-bet check.
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "PERF_GUARD_FLOOR", 0.50)
        monkeypatch.setattr(bot, "PERF_GUARD_DERATE", 0.0)
        monkeypatch.setattr(bot, "live_wins", 8)
        monkeypatch.setattr(bot, "live_losses", 22)
        assert bot.performance_guard_multiplier() == 0.0


class TestUpdateLivePrior:
    def test_prior_shifts_toward_empirical(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        monkeypatch.setattr(bot, "live_wins", 30)
        monkeypatch.setattr(bot, "live_losses", 20)
        bot._live_prior = 0.635
        bot.update_live_prior()
        # 30/50 = 0.60 empirical, weight = 50/50 = 1.0 → prior fully = empirical
        assert abs(bot._live_prior - 0.60) < 0.01

    def test_prior_unchanged_below_10(self, monkeypatch):
        monkeypatch.setattr(bot, "live_wins", 5)
        monkeypatch.setattr(bot, "live_losses", 4)
        bot._live_prior = 0.635
        bot.update_live_prior()
        assert bot._live_prior == 0.635


# ═════════════════════════════════════════════════════════════════════════════
# TIME-OF-DAY LEARNED PRIOR  (per-bucket Bayesian calibration — afternoon fix)
# ═════════════════════════════════════════════════════════════════════════════

class TestBucketStats:
    """The persistent per-time-of-day prior that learns the afternoon is worse.
    Exercised in isolation from the module singleton (persist=False / tmp_path)."""

    def _bs(self, persist=False, path="unused.json"):
        return bot.BucketStats(path=path, persist=persist)

    # ── bucketing ──────────────────────────────────────────────────────────────
    def test_key_groups_hours(self, monkeypatch):
        monkeypatch.setattr(bot, "BUCKET_GROUP_HOURS", 3)
        # 18:00–20:59 UTC (the mean-reverting US afternoon) share one bucket.
        assert bot.BucketStats.key_for_hour(18) == "18-20"
        assert bot.BucketStats.key_for_hour(19) == "18-20"
        assert bot.BucketStats.key_for_hour(20) == "18-20"
        assert bot.BucketStats.key_for_hour(21) == "21-23"
        assert bot.BucketStats.key_for_hour(0)  == "00-02"

    # ── prior_for blend ──────────────────────────────────────────────────────────
    def test_empty_bucket_is_base_accuracy(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        prior, n = self._bs().prior_for("18-20")
        assert prior == 0.635 and n == 0

    def test_thin_sample_shrinks_toward_base(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        monkeypatch.setattr(bot, "BUCKET_PRIOR_FULL_N", 30)
        bs = self._bs()
        bs.record("18-20", won=False)   # 0W/1L, weight = 1/30 → barely moves
        prior, n = bs.prior_for("18-20")
        assert n == 1 and prior < 0.635 and prior > 0.60

    def test_large_losing_sample_drops_prior(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        monkeypatch.setattr(bot, "BUCKET_PRIOR_FULL_N", 30)
        bs = self._bs()
        for _ in range(10):
            bs.record("18-20", won=True)
        for _ in range(40):
            bs.record("18-20", won=False)
        prior, n = bs.prior_for("18-20")
        # n=50 ≥ full_N → weight 1.0 → prior == empirical 10/50 = 0.20.
        assert n == 50 and abs(prior - 0.20) < 1e-9

    def test_large_winning_sample_lifts_prior(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        monkeypatch.setattr(bot, "BUCKET_PRIOR_FULL_N", 30)
        bs = self._bs()
        for _ in range(40):
            bs.record("14-16", won=True)
        for _ in range(10):
            bs.record("14-16", won=False)
        prior, _ = bs.prior_for("14-16")
        assert prior > 0.635

    def test_record_ignores_missing_key(self):
        bs = self._bs()
        bs.record(None, won=False)   # unmatched pre-restart trade → no-op
        bs.record("", won=True)
        assert bs.prior_for(None) == (bot.OB_BASE_ACCURACY, 0)

    # ── persistence ──────────────────────────────────────────────────────────────
    def test_save_load_round_trip(self, tmp_path):
        p = str(tmp_path / "bucket_stats.json")
        bs = bot.BucketStats(path=p, persist=True)
        bs.record("18-20", won=False)
        bs.record("18-20", won=False)
        bs.record("18-20", won=True)
        # A fresh instance must read the accumulated tally back from disk — this
        # is what lets the afternoon bleed accumulate across daily restarts.
        reloaded = bot.BucketStats(path=p, persist=True)
        _, n = reloaded.prior_for("18-20")
        assert n == 3

    def test_missing_file_self_heals(self, tmp_path):
        p = str(tmp_path / "does_not_exist.json")
        bs = bot.BucketStats(path=p, persist=True)   # no crash on absent file
        assert bs.prior_for("18-20") == (bot.OB_BASE_ACCURACY, 0)

    def test_corrupt_file_self_heals(self, tmp_path):
        p = tmp_path / "bucket_stats.json"
        p.write_text("{not valid json")
        bs = bot.BucketStats(path=str(p), persist=True)   # no crash on garbage
        assert bs.prior_for("18-20") == (bot.OB_BASE_ACCURACY, 0)


class TestBucketPriorGatesAfternoon:
    """The learned afternoon prior must flow through win_prob into the edge gate
    so a poor-performing bucket stops trading — the actual mitigation."""

    def _ob(self):
        return {"imbalance": 0.71, "total_depth": 34000.0, "eff_thresh": 0.66}

    def _losing_now(self, monkeypatch):
        bs  = bot.BucketStats(path="unused.json", persist=False)
        key = bs.key_now()                       # whatever bucket "now" falls in
        for _ in range(10):
            bs.record(key, won=True)
        for _ in range(40):
            bs.record(key, won=False)            # 10W/40L → prior 0.20
        monkeypatch.setattr(bot, "bucket_stats", bs)

    def _empty_now(self, monkeypatch):
        monkeypatch.setattr(
            bot, "bucket_stats",
            bot.BucketStats(path="unused.json", persist=False))

    def test_losing_bucket_lowers_win_prob(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        monkeypatch.setattr(bot, "BUCKET_PRIOR_FULL_N", 30)
        args = (self._ob(), "AGREE", 0.045, bot.Regime.TRENDING_UP, 0.78, 0.0)

        self._empty_now(monkeypatch)
        wp_empty = bot.bayesian_win_prob(*args)
        self._losing_now(monkeypatch)
        wp_losing = bot.bayesian_win_prob(*args)

        assert wp_losing < wp_empty

    def test_losing_bucket_flips_edge_gate(self, monkeypatch):
        monkeypatch.setattr(bot, "OB_BASE_ACCURACY", 0.635)
        monkeypatch.setattr(bot, "BUCKET_PRIOR_FULL_N", 30)
        monkeypatch.setattr(bot, "MIN_EDGE_PCT", 0.06)
        args  = (self._ob(), "AGREE", 0.045, bot.Regime.TRENDING_UP, 0.78, 0.0)
        price = 59   # the cent price of the 2026-06-29 14:00-ET losing trade

        self._empty_now(monkeypatch)
        edge_empty = bot.calc_edge(bot.bayesian_win_prob(*args), price)
        self._losing_now(monkeypatch)
        edge_losing = bot.calc_edge(bot.bayesian_win_prob(*args), price)

        # Same setup: a fresh bucket clears the edge gate and trades; a bucket
        # with a proven losing record falls below it and skips.
        assert edge_empty >= bot.MIN_EDGE_PCT
        assert edge_losing < bot.MIN_EDGE_PCT


# ═════════════════════════════════════════════════════════════════════════════
# PEM NORMALIZATION
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizePem:
    def test_standard_pem(self):
        result = bot._normalize_pem(_test_pem)
        assert "-----BEGIN PRIVATE KEY-----" in result
        assert "-----END PRIVATE KEY-----" in result

    def test_escaped_newlines(self):
        raw = _test_pem.replace("\n", "\\n")
        result = bot._normalize_pem(raw)
        assert "-----BEGIN PRIVATE KEY-----\n" in result

    def test_no_newlines(self):
        raw = _test_pem.replace("\n", "")
        result = bot._normalize_pem(raw)
        assert "-----BEGIN PRIVATE KEY-----\n" in result

    def test_invalid_pem_raises(self):
        with pytest.raises(ValueError, match="missing header/footer"):
            bot._normalize_pem("not a pem at all")


# ═════════════════════════════════════════════════════════════════════════════
# PAPER MODE ARITHMETIC — direct unit test
# ═════════════════════════════════════════════════════════════════════════════

class TestPaperModeAccounting:
    """
    Verify the net balance effect of a paper trade lifecycle.
    WIN:  entry deducts cost, settlement adds count → net = count - cost = profit
    LOSS: entry deducts cost, settlement adds nothing → net = -cost
    """

    def test_win_net_is_positive_profit(self):
        price_cents = 50
        count = 4
        cost = price_cents * count / 100.0  # $2.00

        start_balance = 25.0
        after_entry   = start_balance - cost        # $23.00
        after_win     = after_entry + count          # $23.00 + 4 = $27.00
        net            = after_win - start_balance   # +$2.00

        pnl = round(count - cost, 2)  # 4 - 2 = $2.00
        assert net == pnl
        assert net > 0

    def test_loss_net_is_negative_cost(self):
        price_cents = 50
        count = 4
        cost = price_cents * count / 100.0  # $2.00

        start_balance = 25.0
        after_entry   = start_balance - cost        # $23.00
        after_loss    = after_entry                  # no change
        net            = after_loss - start_balance  # -$2.00

        pnl = round(-cost, 2)  # -$2.00
        assert net == pnl
        assert net < 0

    def test_win_not_double_deducting_cost(self):
        """Regression: v8.x bug was paper_balance += (count - cost) instead of += count."""
        price_cents = 50
        count = 4
        cost = price_cents * count / 100.0

        start_balance = 25.0
        after_entry   = start_balance - cost
        # CORRECT: add full payout
        correct_win   = after_entry + count
        # BUG: add only profit
        buggy_win     = after_entry + (count - cost)

        assert correct_win != buggy_win
        assert correct_win - start_balance == count - cost  # net = profit
        assert buggy_win - start_balance == 0.0             # net = $0 (wrong)


class TestUpdateSessionState:
    """v9.4.0: RECOVERY mode was removed by owner directive. update_session_state
    is now a no-op — a drawdown must NOT flip the session into RECOVERY (which
    would have shrunk the stake)."""

    def setup_method(self):
        bot.session_state = SessionState.ACTIVE

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def test_deep_drawdown_does_not_enter_recovery(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        bot.session_state = SessionState.ACTIVE
        bot.update_session_state(1750.0)  # 12.5% loss — would have triggered RECOVERY
        assert bot.session_state == SessionState.ACTIVE

    def test_active_stays_active(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        bot.session_state = SessionState.ACTIVE
        bot.update_session_state(1990.0)  # tiny loss
        assert bot.session_state == SessionState.ACTIVE


# ═════════════════════════════════════════════════════════════════════════════
# v9.5.0: RECOVERY MODE (two-tier sizing)
# ═════════════════════════════════════════════════════════════════════════════

class TestRecoveryState:
    """Unit tests for the persistent RecoveryState transitions, in isolation
    from the module-level singleton (persist=False, no disk I/O)."""

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def _rs(self):
        return bot.RecoveryState(path="unused.json", persist=False)

    def test_enter_sets_target_and_active(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        assert rs.enter(target_balance=10_000.0, current_balance=9_500.0) is True
        assert rs.active is True
        assert rs.target_balance == 10_000.0

    def test_enter_noop_when_already_active(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        rs.enter(10_000.0, 9_500.0)
        # A second (deeper) loss must NOT move the target.
        assert rs.enter(9_500.0, 9_000.0) is False
        assert rs.target_balance == 10_000.0

    def test_enter_noop_when_already_at_target(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        # Nothing to recover if balance already ≥ target.
        assert rs.enter(10_000.0, 10_000.0) is False
        assert rs.active is False

    def test_enter_rejects_bad_target(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        assert rs.enter(0.0, 9_500.0) is False
        assert rs.enter(None, 9_500.0) is False
        assert rs.active is False

    def test_maybe_exit_below_target_stays(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        rs.enter(10_000.0, 9_500.0)
        assert rs.maybe_exit(9_999.99) is False
        assert rs.active is True

    def test_maybe_exit_at_target_exits(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        rs.enter(10_000.0, 9_500.0)
        assert rs.maybe_exit(10_000.0) is True   # >= is the boundary
        assert rs.active is False
        assert rs.target_balance == 0.0

    def test_maybe_exit_noop_when_inactive(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        assert rs.maybe_exit(10_000.0) is False

    def test_reconcile_clears_when_already_recovered(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        rs.active = True
        rs.target_balance = 10_000.0
        rs.reconcile_on_boot(10_500.0)  # came back up while bot was down
        assert rs.active is False

    def test_reconcile_resumes_when_still_below(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        rs.active = True
        rs.target_balance = 10_000.0
        rs.reconcile_on_boot(9_600.0)
        assert rs.active is True
        assert rs.target_balance == 10_000.0

    def test_reconcile_clears_corrupt_target(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rs = self._rs()
        rs.active = True
        rs.target_balance = 0.0
        rs.reconcile_on_boot(9_600.0)
        assert rs.active is False

    def test_persistence_round_trip(self, monkeypatch, tmp_path):
        self._no_telegram(monkeypatch)
        path = str(tmp_path / "recovery_state.json")
        rs1 = bot.RecoveryState(path=path, persist=True)
        rs1.enter(10_000.0, 9_500.0)
        # A fresh instance loads the persisted state (survives process restart).
        rs2 = bot.RecoveryState(path=path, persist=True)
        assert rs2.active is True
        assert rs2.target_balance == 10_000.0
        rs1.maybe_exit(10_000.0)
        rs3 = bot.RecoveryState(path=path, persist=True)
        assert rs3.active is False


class TestActiveTradeSize:
    def setup_method(self):
        bot.recovery.active = False
        bot.recovery.target_balance = 0.0

    def test_normal_size_when_inactive(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        assert bot.active_trade_size() == 500.0

    def test_recovery_size_when_active(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        bot.recovery.active = True
        assert bot.active_trade_size() == 100.0


class TestOnTradeSettledRecoveryEntry:
    """The settlement hook: only a normal-mode (full-size) loss arms recovery."""

    def setup_method(self):
        bot.recovery.active = False
        bot.recovery.target_balance = 0.0

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def test_full_size_loss_activates(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rec = {"mode_at_entry": "normal", "balance_before": 10_000.0}
        bot.on_trade_settled(won=False, trade_rec=rec, current_balance=9_500.0)
        assert bot.recovery.active is True
        assert bot.recovery.target_balance == 10_000.0

    def test_win_does_not_activate(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rec = {"mode_at_entry": "normal", "balance_before": 10_000.0}
        bot.on_trade_settled(won=True, trade_rec=rec, current_balance=10_500.0)
        assert bot.recovery.active is False

    def test_recovery_size_loss_does_not_activate(self, monkeypatch):
        self._no_telegram(monkeypatch)
        rec = {"mode_at_entry": "recovery", "balance_before": 9_400.0}
        bot.on_trade_settled(won=False, trade_rec=rec, current_balance=9_300.0)
        assert bot.recovery.active is False

    def test_loss_while_already_recovering_keeps_target(self, monkeypatch):
        self._no_telegram(monkeypatch)
        bot.recovery.active = True
        bot.recovery.target_balance = 10_000.0
        rec = {"mode_at_entry": "recovery", "balance_before": 9_400.0}
        bot.on_trade_settled(won=False, trade_rec=rec, current_balance=9_300.0)
        assert bot.recovery.target_balance == 10_000.0

    def test_missing_fields_do_not_activate(self, monkeypatch):
        self._no_telegram(monkeypatch)
        # Pre-upgrade / unattributable trades have no mode_at_entry.
        bot.on_trade_settled(won=False, trade_rec={}, current_balance=9_500.0)
        assert bot.recovery.active is False

    def test_full_cycle_enter_then_exit(self, monkeypatch):
        self._no_telegram(monkeypatch)
        # 1) full-size loss → recovery on, target = pre-trade balance
        rec = {"mode_at_entry": "normal", "balance_before": 10_000.0}
        bot.on_trade_settled(won=False, trade_rec=rec, current_balance=9_500.0)
        assert bot.recovery.active is True
        # 2) balance climbs back at reduced size but not yet to target
        assert bot.recovery.maybe_exit(9_800.0) is False
        assert bot.recovery.active is True
        # 3) balance reaches target → auto-resume normal
        assert bot.recovery.maybe_exit(10_000.0) is True
        assert bot.recovery.active is False

    def test_recovery_exit_pauses_ladder_size_up(self, monkeypatch):
        """Exiting recovery holds the ladder at baseline for N fresh trades so
        it re-proves the edge before scaling the stake above NORMAL again."""
        from ladder import StakeLadder, LadderConfig
        self._no_telegram(monkeypatch)
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=5,
                                           window=20, cooldown_secs=0))
        monkeypatch.setattr(bot, "stake_ladder", lad)
        monkeypatch.setattr(bot, "RECOVERY_LADDER_PAUSE_TRADES", 5)
        # Hot record → the ladder would size up if not paused.
        for _ in range(9):
            lad.on_trade_result(True, 2.0)
        assert lad.get_stake(5.0).multiplier > 1.0

        # Arm recovery, then exit at target → pause must engage.
        bot.recovery.active = True
        bot.recovery.target_balance = 10_000.0
        assert bot.recovery.maybe_exit(10_000.0) is True
        for _ in range(5):
            assert lad.get_stake(5.0).multiplier == 1.0   # held at baseline
            lad.on_trade_result(True, 2.0)
        assert lad.get_stake(5.0).multiplier > 1.0        # resumes after 5

    def test_keep_normal_stake_no_size_drop_in_recovery(self, monkeypatch):
        """RECOVERY_KEEP_NORMAL_STAKE: while recovery is active the stake stays on
        the NORMAL ladder size instead of dropping to RECOVERY_TRADE_SIZE."""
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        bot.probation.cancel()
        bot.recovery.active = True
        bot.recovery.target_balance = 10_000.0
        # Default (flag OFF) drops to recovery size.
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", False)
        assert bot.active_trade_size() == 100.0
        # Flag ON keeps the normal stake even though recovery is active.
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", True)
        assert bot.active_trade_size() == 500.0
        assert bot.recovery.active is True                # still tracking recovery

    def test_keep_normal_stake_lifts_ladder_clawback_cap(self, monkeypatch):
        """RECOVERY_KEEP_NORMAL_STAKE: recovery no longer forces the in_clawback
        1x ladder cap, so a hot ladder can size up during recovery."""
        from ladder import StakeLadder, LadderConfig
        self._no_telegram(monkeypatch)
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=5,
                                           window=20, cooldown_secs=0))
        monkeypatch.setattr(bot, "stake_ladder", lad)
        for _ in range(9):
            lad.on_trade_result(True, 2.0)
        bot.recovery.active = True
        bot.recovery.target_balance = 10_000.0
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", False)
        assert bot.in_clawback() is True                  # recovery caps the ladder
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", True)
        assert bot.in_clawback() is False                 # cap lifted by the flag

    def test_keep_normal_stake_exit_skips_probation_and_pause(self, monkeypatch):
        """RECOVERY_KEEP_NORMAL_STAKE: the stake never dropped, so exiting recovery
        starts NO probation ramp and pauses NO ladder size-up — sizing carries on
        unchanged. Recovery still exits on its existing balance rule."""
        from ladder import StakeLadder, LadderConfig
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", True)
        monkeypatch.setattr(bot, "RECOVERY_LADDER_PAUSE_TRADES", 5)
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=5,
                                           window=20, cooldown_secs=0))
        monkeypatch.setattr(bot, "stake_ladder", lad)
        for _ in range(9):
            lad.on_trade_result(True, 2.0)
        assert lad.get_stake(5.0).multiplier > 1.0

        bot.probation.cancel()
        bot.recovery.active = True
        bot.recovery.target_balance = 10_000.0
        assert bot.recovery.maybe_exit(10_000.0) is True  # exits on the same rules
        assert bot.recovery.active is False
        bot.resume_after_recovery()
        assert bot.probation.active is False              # no ramp
        assert lad.get_stake(5.0).multiplier > 1.0        # no size-up pause

    def test_keep_normal_stake_off_preserves_probation_ramp(self, monkeypatch):
        """Default (flag OFF): exiting recovery still begins the graduated
        probation ramp — existing behavior is untouched."""
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", False)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "")
        bot.probation.cancel()
        bot.resume_after_recovery()
        assert bot.probation.active is True
        assert bot.active_trade_size() < 500.0            # sub-full ramp base

    def test_keep_normal_stake_messages_report_no_change(self, monkeypatch):
        """The activation/exit notifications must report exactly what happens:
        recovery triggered, stake unchanged. They must NOT claim a size switch
        to RECOVERY_TRADE_SIZE, and must state the real (variable) normal base."""
        sent = []
        monkeypatch.setattr(bot.tg, "send_telegram_message",
                            lambda m, *a, **k: sent.append(m) or True)
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", True)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 250.0)   # base from the var
        rs = bot.RecoveryState(path="unused.json", persist=False)

        assert rs.enter(target_balance=10_000.0, current_balance=9_500.0) is True
        enter_msg = sent[-1]
        assert "$250.00" in enter_msg                       # real normal base
        assert "$100.00" not in enter_msg                   # never the recovery size
        assert "tracking only" in enter_msg.lower()

        assert rs.maybe_exit(10_000.0) is True
        exit_msg = sent[-1]
        assert "$250.00" in exit_msg
        assert "$100.00" not in exit_msg
        assert "nothing changed" in exit_msg.lower()

    def test_off_mode_messages_unchanged(self, monkeypatch):
        """Default (flag OFF): the existing two-tier messages are preserved —
        activation still reports the switch to RECOVERY_TRADE_SIZE."""
        sent = []
        monkeypatch.setattr(bot.tg, "send_telegram_message",
                            lambda m, *a, **k: sent.append(m) or True)
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", False)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 250.0)
        rs = bot.RecoveryState(path="unused.json", persist=False)
        assert rs.enter(target_balance=10_000.0, current_balance=9_500.0) is True
        assert "$100.00" in sent[-1]                        # switch to recovery size
        assert "ACTIVATED" in sent[-1]


# ═════════════════════════════════════════════════════════════════════════════
# RECOVERY WIN-RATE STEP-UP (owner directive — size back to full on a strong WR)
# ═════════════════════════════════════════════════════════════════════════════

class TestRecoveryWinRateStepUp:
    """While recovery has cut the stake to RECOVERY_TRADE_SIZE, a strong recovery
    win rate (> threshold, over a min sample) sizes the next entry back to full
    NORMAL_TRADE_SIZE. Re-evaluated every settlement; off by default."""

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def _armed_recovery(self, monkeypatch):
        """A fresh, active RecoveryState wired as the module singleton, with the
        step-up enabled at 65% / min-4 and sizes 100→250."""
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "RECOVERY_WINRATE_STEPUP", True)
        monkeypatch.setattr(bot, "RECOVERY_STEPUP_WINRATE", 0.65)
        monkeypatch.setattr(bot, "RECOVERY_STEPUP_MIN_TRADES", 4)
        monkeypatch.setattr(bot, "RECOVERY_KEEP_NORMAL_STAKE", False)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 250.0)
        rs = bot.RecoveryState(path="unused.json", persist=False)
        rs.enter(target_balance=10_000.0, current_balance=9_500.0)
        monkeypatch.setattr(bot, "recovery", rs)
        return rs

    _REC = {"mode_at_entry": "recovery"}

    def test_below_min_trades_stays_reduced(self, monkeypatch):
        rs = self._armed_recovery(monkeypatch)
        for _ in range(3):                       # 3 wins, total < min 4
            rs.record_result(True, self._REC)
        assert rs.stepup_active() is False
        assert bot.active_trade_size() == 100.0

    def test_steps_up_to_full_on_strong_winrate(self, monkeypatch):
        rs = self._armed_recovery(monkeypatch)
        for _ in range(4):                       # 4/4 = 100% > 65%
            rs.record_result(True, self._REC)
        assert rs.stepup_active() is True
        assert bot.active_trade_size() == 250.0  # full normal on the next entry

    def test_reverts_when_winrate_falls_back(self, monkeypatch):
        rs = self._armed_recovery(monkeypatch)
        for _ in range(4):
            rs.record_result(True, self._REC)
        assert bot.active_trade_size() == 250.0
        rs.record_result(False, self._REC)       # 4/5 = 80% > 65%
        assert bot.active_trade_size() == 250.0
        rs.record_result(False, self._REC)       # 4/6 = 66.7% > 65%
        assert bot.active_trade_size() == 250.0
        rs.record_result(False, self._REC)       # 4/7 = 57.1% <= 65% → revert
        assert rs.stepup_active() is False
        assert bot.active_trade_size() == 100.0

    def test_only_recovery_tagged_trades_count(self, monkeypatch):
        rs = self._armed_recovery(monkeypatch)
        for _ in range(6):                       # 6 normal-tagged wins: ignored
            rs.record_result(True, {"mode_at_entry": "normal"})
        assert rs.period_wins == 0 and rs.period_losses == 0
        assert rs.stepup_active() is False
        assert bot.active_trade_size() == 100.0

    def test_boundary_is_strict_greater_than(self, monkeypatch):
        rs = self._armed_recovery(monkeypatch)
        # Exactly 65% (13/20) must NOT step up — the gate is strictly greater.
        for _ in range(13):
            rs.record_result(True, self._REC)
        for _ in range(7):
            rs.record_result(False, self._REC)
        assert (rs.period_wins, rs.period_losses) == (13, 7)
        assert rs.stepup_active() is False
        assert bot.active_trade_size() == 100.0

    def test_disabled_by_default(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "RECOVERY_WINRATE_STEPUP", False)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 250.0)
        rs = bot.RecoveryState(path="unused.json", persist=False)
        rs.enter(target_balance=10_000.0, current_balance=9_500.0)
        monkeypatch.setattr(bot, "recovery", rs)
        for _ in range(10):                      # perfect record, feature off
            rs.record_result(True, self._REC)
        assert rs.period_wins == 0               # not even tracked when off
        assert rs.stepup_active() is False
        assert bot.active_trade_size() == 100.0

    def test_resets_on_new_recovery(self, monkeypatch):
        rs = self._armed_recovery(monkeypatch)
        for _ in range(4):
            rs.record_result(True, self._REC)
        assert rs.stepup_active() is True
        rs.maybe_exit(10_000.0)                   # exit clears the window
        assert rs.period_wins == 0 and rs.period_losses == 0
        rs.enter(target_balance=11_000.0, current_balance=10_500.0)
        assert rs.stepup_active() is False        # fresh window, reduced size
        assert bot.active_trade_size() == 100.0

    def test_stepup_survives_persistence_roundtrip(self, monkeypatch, tmp_path):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "RECOVERY_WINRATE_STEPUP", True)
        monkeypatch.setattr(bot, "RECOVERY_STEPUP_WINRATE", 0.65)
        monkeypatch.setattr(bot, "RECOVERY_STEPUP_MIN_TRADES", 4)
        p = str(tmp_path / "rec.json")
        rs = bot.RecoveryState(path=p, persist=True)
        rs.enter(target_balance=10_000.0, current_balance=9_500.0)
        for _ in range(4):
            rs.record_result(True, self._REC)
        # Reload from disk → the recovery-period window is restored.
        rs2 = bot.RecoveryState(path=p, persist=True)
        assert (rs2.period_wins, rs2.period_losses) == (4, 0)
        assert rs2.stepup_active() is True


# ═════════════════════════════════════════════════════════════════════════════
# PROBATION RAMP (post-recovery graduated re-entry — 2026-06-29 log-review fix)
# ═════════════════════════════════════════════════════════════════════════════

class TestProbationRungs:
    """The ramp's sub-full base sizes: auto-built or explicitly overridden,
    always clamped to [RECOVERY_TRADE_SIZE, NORMAL_TRADE_SIZE)."""

    def test_auto_default_is_floor_and_half(self, monkeypatch):
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "")
        assert bot._probation_rungs() == [100.0, 250.0]

    def test_explicit_override_prepends_floor(self, monkeypatch):
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "150,300")
        assert bot._probation_rungs() == [100.0, 150.0, 300.0]

    def test_override_clamps_out_of_range(self, monkeypatch):
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        # 50 < floor (dropped); 500/900 ≥ full (dropped); floor prepended.
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "50,250,500,900")
        assert bot._probation_rungs() == [100.0, 250.0]

    def test_no_room_when_normal_le_recovery(self, monkeypatch):
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "")
        assert bot._probation_rungs() == []


class TestProbationState:
    """Unit tests for the graduated-re-entry ramp in isolation (persist=False)."""

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def _ps(self):
        return bot.ProbationState(path="unused.json", persist=False)

    def _cfg(self, monkeypatch, streak=2, wr=0.60, wr_n=4, enabled=True,
             normal=500.0, gate_size=500.0, min_balance=5000.0):
        monkeypatch.setattr(bot, "PROBATION_RAMP_ENABLED", enabled)
        monkeypatch.setattr(bot, "PROBATION_WIN_STREAK", streak)
        monkeypatch.setattr(bot, "PROBATION_WIN_RATE_MIN", wr)
        monkeypatch.setattr(bot, "PROBATION_WINRATE_MIN_TRADES", wr_n)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", normal)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "HIGH_STAKE_GATE_SIZE", gate_size)
        monkeypatch.setattr(bot, "HIGH_STAKE_MIN_BALANCE", min_balance)

    def test_start_sets_floor_and_active(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        ps = self._ps()
        assert ps.start([100.0, 250.0], 500.0) is True
        assert ps.active is True
        assert ps.current_size() == 100.0

    def test_start_noop_when_disabled(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch, enabled=False)
        ps = self._ps()
        assert ps.start([100.0, 250.0], 500.0) is False
        assert ps.active is False

    def test_start_noop_when_no_subfull_rungs(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        ps = self._ps()
        assert ps.start([], 500.0) is False
        assert ps.start([500.0], 500.0) is False   # only a full-size rung → no room

    def test_win_streak_advances_one_rung(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch, streak=2)
        ps = self._ps(); ps.start([100.0, 250.0], 500.0)
        ps.record_result(True)               # streak 1 — not yet
        assert ps.current_size() == 100.0
        ps.record_result(True)               # streak 2 — advance
        assert ps.current_size() == 250.0
        assert ps.level == 1
        assert ps.streak == 0                # must re-prove at the larger size

    def test_loss_steps_down_one_rung(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch, streak=2)
        ps = self._ps(); ps.start([100.0, 250.0], 500.0)
        ps.record_result(True); ps.record_result(True)   # → rung 250
        assert ps.current_size() == 250.0
        ps.record_result(False)              # loss → step down to floor
        assert ps.current_size() == 100.0
        assert ps.level == 0

    def test_loss_at_floor_holds(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        ps = self._ps(); ps.start([100.0, 250.0], 500.0)
        ps.record_result(False)
        assert ps.active is True
        assert ps.current_size() == 100.0    # never below the recovery floor

    def test_graduates_at_top_rung(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch, streak=2)
        ps = self._ps(); ps.start([100.0, 250.0], 500.0)
        ps.record_result(True); ps.record_result(True)   # floor → 250 (top rung)
        ps.record_result(True); ps.record_result(True)   # top rung proven → graduate
        assert ps.active is False
        assert ps.current_size() == 500.0    # full size restored

    def test_winrate_path_advances_without_streak(self, monkeypatch):
        # Streak gate unreachable; only the rolling win-rate path can fire.
        self._no_telegram(monkeypatch)
        self._cfg(monkeypatch, streak=99, wr=0.60, wr_n=4)
        ps = self._ps(); ps.start([100.0, 250.0], 500.0)
        ps.record_result(True)               # n=1
        ps.record_result(True)               # n=2
        ps.record_result(False)              # n=3 (floor hold), 2/3
        assert ps.level == 0
        ps.record_result(True)               # n=4, 3/4=75% ≥ 60% → advance
        assert ps.level == 1

    def test_cancel_drops_ramp(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        ps = self._ps(); ps.start([100.0, 250.0], 500.0)
        ps.cancel()
        assert ps.active is False
        assert ps.current_size() == 500.0

    def test_reconcile_clamps_level(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        ps = self._ps()
        ps.active = True; ps.rungs = [100.0, 250.0]; ps.full_size = 500.0
        ps.level = 9                         # corrupt
        ps.reconcile_on_boot()
        assert ps.level == 1

    def test_reconcile_clears_corrupt_ramp(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        ps = self._ps()
        ps.active = True; ps.rungs = []; ps.full_size = 0.0
        ps.reconcile_on_boot()
        assert ps.active is False

    def test_persistence_round_trip(self, monkeypatch, tmp_path):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        path = str(tmp_path / "probation_state.json")
        ps1 = bot.ProbationState(path=path, persist=True)
        ps1.start([100.0, 250.0], 500.0)
        ps1.record_result(True); ps1.record_result(True)   # → rung 250
        ps2 = bot.ProbationState(path=path, persist=True)
        assert ps2.active is True
        assert ps2.current_size() == 250.0
        assert ps2.level == 1

    def test_start_records_arm_day(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        ps = self._ps(); ps.start([100.0, 250.0], 500.0)
        today = bot.datetime.now(bot.timezone.utc).strftime("%Y-%m-%d")
        assert ps.day == today

    def test_reconcile_new_day_rearms_to_floor(self, monkeypatch):
        # v9.7.0: a restart crossing midnight re-arms the ramp from the floor.
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "")
        bot.recovery.active = False
        ps = self._ps()
        ps.active = True; ps.rungs = [100.0, 250.0]; ps.full_size = 500.0
        ps.level = 1; ps.day = "2000-01-01"        # stale → new day
        ps.reconcile_on_boot()
        assert ps.active is True
        assert ps.level == 0
        assert ps.current_size() == 100.0
        assert ps.day == bot.datetime.now(bot.timezone.utc).strftime("%Y-%m-%d")

    def test_reconcile_same_day_resumes_progress(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        bot.recovery.active = False
        today = bot.datetime.now(bot.timezone.utc).strftime("%Y-%m-%d")
        ps = self._ps()
        ps.active = True; ps.rungs = [100.0, 250.0]; ps.full_size = 500.0
        ps.level = 1; ps.day = today               # same day → no reset
        ps.reconcile_on_boot()
        assert ps.level == 1
        assert ps.current_size() == 250.0

    def test_reconcile_new_day_skipped_in_recovery(self, monkeypatch):
        self._no_telegram(monkeypatch); self._cfg(monkeypatch)
        bot.recovery.active = True
        try:
            ps = self._ps()
            ps.active = True; ps.rungs = [100.0, 250.0]; ps.full_size = 500.0
            ps.level = 1; ps.day = "2000-01-01"
            ps.reconcile_on_boot()
            assert ps.level == 1                   # recovery deeper → ramp untouched
        finally:
            bot.recovery.active = False

    # ── v9.8.0: balance-gated advancement to the high ($750/$1000) rungs ────────
    def test_advance_into_high_rung_blocked_below_min_balance(self, monkeypatch):
        # At the $500 rung with the win gate met, the next rung ($750) is gated:
        # below $5000 the ramp HOLDS at $500 and keeps the streak.
        self._no_telegram(monkeypatch)
        self._cfg(monkeypatch, streak=2, normal=1000.0)
        ps = self._ps(); ps.start([100.0, 250.0, 500.0, 750.0], 1000.0)
        ps.level = 2                              # sitting at $500
        ps.record_result(True, balance=4000.0)   # streak 1
        ps.record_result(True, balance=4000.0)   # streak 2, gate met — but gated
        assert ps.current_size() == 500.0
        assert ps.level == 2
        assert ps.streak == 2                     # streak preserved for later

    def test_advance_into_high_rung_allowed_at_min_balance(self, monkeypatch):
        self._no_telegram(monkeypatch)
        self._cfg(monkeypatch, streak=2, normal=1000.0)
        ps = self._ps(); ps.start([100.0, 250.0, 500.0, 750.0], 1000.0)
        ps.level = 2
        ps.record_result(True, balance=6000.0)
        ps.record_result(True, balance=6000.0)   # gate met AND balance clears
        assert ps.current_size() == 750.0
        assert ps.level == 3

    def test_graduation_to_full_size_balance_gated(self, monkeypatch):
        # Top rung $750 proven, but graduating to the full $1000 needs ≥ $5000.
        self._no_telegram(monkeypatch)
        self._cfg(monkeypatch, streak=2, normal=1000.0)
        ps = self._ps(); ps.start([100.0, 250.0, 500.0, 750.0], 1000.0)
        ps.level = 3                              # at $750 (already unlocked)
        ps.record_result(True, balance=4000.0)
        ps.record_result(True, balance=4000.0)   # gate met, graduation gated
        assert ps.active is True
        assert ps.current_size() == 750.0
        # Once equity clears, the same proven edge graduates to full $1000.
        ps.record_result(True, balance=6000.0)
        assert ps.active is False
        assert ps.current_size() == 1000.0

    def test_low_rung_advance_never_gated(self, monkeypatch):
        # Climbing $100 → $250 (both ≤ $500) is never balance-gated.
        self._no_telegram(monkeypatch)
        self._cfg(monkeypatch, streak=2, normal=1000.0)
        ps = self._ps(); ps.start([100.0, 250.0, 500.0, 750.0], 1000.0)
        ps.record_result(True, balance=100.0)
        ps.record_result(True, balance=100.0)
        assert ps.current_size() == 250.0         # advanced despite tiny balance

    def test_balance_none_preserves_legacy_advance(self, monkeypatch):
        # No balance in scope (legacy/unit path) must not block advancement.
        self._no_telegram(monkeypatch)
        self._cfg(monkeypatch, streak=2, normal=1000.0)
        ps = self._ps(); ps.start([100.0, 250.0, 500.0, 750.0], 1000.0)
        ps.level = 2
        ps.record_result(True)                    # balance defaults None
        ps.record_result(True)
        assert ps.current_size() == 750.0


class TestProbationRungLadder:
    """v9.8.0: _probation_rungs() builds a fixed-step ladder up to NORMAL."""

    def test_ladder_to_1000(self, monkeypatch):
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "")
        monkeypatch.setattr(bot, "PROBATION_RUNG_STEP", 250.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 1000.0)
        assert bot._probation_rungs() == [100.0, 250.0, 500.0, 750.0]

    def test_ladder_to_500_unchanged(self, monkeypatch):
        # Backward compatible with v9.7.0 (NORMAL=$500 → [100, 250]).
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "")
        monkeypatch.setattr(bot, "PROBATION_RUNG_STEP", 250.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        assert bot._probation_rungs() == [100.0, 250.0]

    def test_explicit_override_still_honored(self, monkeypatch):
        monkeypatch.setattr(bot, "PROBATION_RUNGS_RAW", "100,250,500,750")
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 1000.0)
        assert bot._probation_rungs() == [100.0, 250.0, 500.0, 750.0]


class TestHighStakeBalanceGate:
    """v9.8.0: active_trade_size(balance) caps stakes above the gate size while
    equity is below the high-stake minimum balance."""

    def setup_method(self):
        bot.recovery.active = False
        bot.probation.active = False

    def teardown_method(self):
        bot.recovery.active = False
        bot.probation.active = False

    def _cfg(self, monkeypatch, normal=1000.0, gate=500.0, min_bal=5000.0):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", normal)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        monkeypatch.setattr(bot, "HIGH_STAKE_GATE_SIZE", gate)
        monkeypatch.setattr(bot, "HIGH_STAKE_MIN_BALANCE", min_bal)

    def test_normal_capped_below_min_balance(self, monkeypatch):
        self._cfg(monkeypatch)
        assert bot.active_trade_size(4000.0) == 500.0     # $1000 → capped to $500
        assert bot.active_trade_size(6000.0) == 1000.0    # unlocked

    def test_no_balance_returns_raw_size(self, monkeypatch):
        self._cfg(monkeypatch)
        assert bot.active_trade_size() == 1000.0          # legacy no-arg path

    def test_recovery_floor_never_gated(self, monkeypatch):
        self._cfg(monkeypatch)
        bot.recovery.active = True
        assert bot.active_trade_size(100.0) == 100.0      # tiny balance, still $100

    def test_probation_high_rung_capped(self, monkeypatch):
        self._cfg(monkeypatch)
        bot.probation.active = True
        bot.probation.rungs = [100.0, 250.0, 500.0, 750.0]
        bot.probation.level = 3                           # $750
        bot.probation.full_size = 1000.0
        assert bot.active_trade_size(4000.0) == 500.0
        assert bot.active_trade_size(6000.0) == 750.0

    def test_gate_size_itself_allowed(self, monkeypatch):
        self._cfg(monkeypatch, normal=500.0)
        assert bot.active_trade_size(100.0) == 500.0      # $500 == gate, allowed


class TestActiveTradeSizeProbation:
    """active_trade_size() priority: recovery → probation → normal."""

    def setup_method(self):
        bot.recovery.active = False
        bot.recovery.target_balance = 0.0
        bot.probation.active = False

    def teardown_method(self):
        bot.recovery.active = False
        bot.probation.active = False

    def test_probation_size_when_active(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        bot.probation.active = True
        bot.probation.rungs = [100.0, 250.0]
        bot.probation.level = 0
        bot.probation.full_size = 500.0
        assert bot.active_trade_size() == 100.0
        assert bot.in_clawback() is True

    def test_recovery_takes_precedence(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        bot.recovery.active = True
        bot.probation.active = True
        bot.probation.rungs = [250.0]
        bot.probation.level = 0
        bot.probation.full_size = 500.0
        assert bot.active_trade_size() == 100.0   # recovery wins


class TestClawbackLadderCap:
    """The 2026-06-29 leak: while clawing back (recovery OR probation), the
    laddering overlay must never size the stake UP, even on a hot win record."""

    def setup_method(self):
        bot.recovery.active = False
        bot.probation.active = False

    def teardown_method(self):
        bot.stake_ladder = None
        bot.recovery.active = False
        bot.probation.active = False

    def _hot_ladder(self):
        clk = _FakeClock()
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=10,
                                           max_multiplier=2.0, cooldown_secs=0),
                          clock=clk)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        return lad

    def test_normal_mode_allows_size_up(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 100.0)
        bot.stake_ladder = self._hot_ladder()
        bet = bot.kelly_bet(0.65, 50, 100_000.0)
        assert bet > 100.0   # 2× tier sizes the flat stake up

    def test_recovery_caps_at_base(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        monkeypatch.setattr(bot, "RECOVERY_TRADE_SIZE", 100.0)
        bot.recovery.active = True
        bot.stake_ladder = self._hot_ladder()
        bet = bot.kelly_bet(0.65, 50, 100_000.0)
        assert bet == 100.0   # capped at base — no $200 leak

    def test_probation_caps_at_current_rung(self, monkeypatch):
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        bot.probation.active = True
        bot.probation.rungs = [100.0, 250.0]
        bot.probation.level = 1            # base $250
        bot.probation.full_size = 500.0
        bot.stake_ladder = self._hot_ladder()
        bet = bot.kelly_bet(0.65, 50, 100_000.0)
        assert bet == 250.0   # capped at the current rung, not 2×


class TestProbationRecordHook:
    """The settlement hook only advances/steps for probation-mode trades."""

    def setup_method(self):
        bot.probation._persist = False
        bot.probation.active = False

    def teardown_method(self):
        bot.probation.active = False

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def _start(self, monkeypatch):
        monkeypatch.setattr(bot, "PROBATION_RAMP_ENABLED", True)
        monkeypatch.setattr(bot, "PROBATION_WIN_STREAK", 2)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        bot.probation.start([100.0, 250.0], 500.0)

    def test_probation_trade_advances(self, monkeypatch):
        self._no_telegram(monkeypatch); self._start(monkeypatch)
        rec = {"mode_at_entry": "probation"}
        bot.probation_record(True, rec)
        bot.probation_record(True, rec)
        assert bot.probation.current_size() == 250.0

    def test_non_probation_trade_ignored(self, monkeypatch):
        self._no_telegram(monkeypatch); self._start(monkeypatch)
        bot.probation_record(True, {"mode_at_entry": "normal"})
        bot.probation_record(True, {"mode_at_entry": "normal"})
        assert bot.probation.current_size() == 100.0   # unchanged


class TestTempStakeOverride:
    """TEMPORARY owner directive: a one-way manual stake ramp that preempts every
    other sizing mode until the bankroll first reaches the exit balance, stepping
    up $10 after every 2 consecutive wins, then retires for good."""

    def setup_method(self):
        # The conftest autouse fixture retires the override; re-arm a clean,
        # non-persisting copy with the hardcoded defaults for these tests.
        bot.recovery.active = False
        bot.probation.active = False
        ov = bot.temp_override
        ov._persist = False
        ov.size = 200.0
        ov.streak = ov.wins = ov.losses = 0
        ov.done = False

    def teardown_method(self):
        bot.temp_override.done = True
        bot.recovery.active = False
        bot.probation.active = False

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def _defaults(self, monkeypatch):
        monkeypatch.setattr(bot, "TEMP_OVERRIDE_ENABLED", True)
        monkeypatch.setattr(bot, "TEMP_OVERRIDE_BASE", 200.0)
        monkeypatch.setattr(bot, "TEMP_OVERRIDE_STEP", 10.0)
        monkeypatch.setattr(bot, "TEMP_OVERRIDE_WIN_STREAK", 2)
        monkeypatch.setattr(bot, "TEMP_OVERRIDE_EXIT_BALANCE", 5000.0)

    def test_starts_at_200(self, monkeypatch):
        self._defaults(monkeypatch)
        assert bot.temp_override.active is True
        assert bot.active_trade_size(1000.0) == 200.0

    def test_preempts_recovery_and_probation(self, monkeypatch):
        self._defaults(monkeypatch)
        bot.recovery.active = True            # would normally force RECOVERY_TRADE_SIZE
        bot.probation.active = True
        assert bot.active_trade_size(1000.0) == 200.0
        assert bot.in_clawback() is True

    def test_steps_up_10_after_two_wins(self, monkeypatch):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        bot.temp_override.record_result(True, 1000.0)
        assert bot.temp_override.current_size() == 200.0   # one win — no step yet
        bot.temp_override.record_result(True, 1000.0)
        assert bot.temp_override.current_size() == 210.0   # two-win streak → +$10
        bot.temp_override.record_result(True, 1000.0)
        bot.temp_override.record_result(True, 1000.0)
        assert bot.temp_override.current_size() == 220.0   # next two wins → +$10

    def test_loss_resets_streak_but_not_size(self, monkeypatch):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        bot.temp_override.record_result(True, 1000.0)
        bot.temp_override.record_result(False, 1000.0)     # breaks the streak
        bot.temp_override.record_result(True, 1000.0)
        assert bot.temp_override.current_size() == 200.0   # only 1 win since loss
        bot.temp_override.record_result(True, 1000.0)
        assert bot.temp_override.current_size() == 210.0   # now a clean 2-win run

    def test_retires_at_exit_balance(self, monkeypatch):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        assert bot.active_trade_size(4999.0) == 200.0      # still under the line
        assert bot.temp_override.active is True
        # Equity reaches $5000 → override retires permanently this run.
        bot.active_trade_size(5000.0)
        assert bot.temp_override.active is False
        assert bot.temp_override.done is True

    def test_reverts_to_normal_after_retirement(self, monkeypatch):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        bot.temp_override.record_result(True, 5000.0)      # win that crosses $5k
        assert bot.temp_override.active is False
        assert bot.active_trade_size(5200.0) == 500.0      # back to the ladder

    def test_record_after_retirement_is_noop(self, monkeypatch):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        bot.temp_override.done = True
        bot.temp_override.record_result(True, 1000.0)
        bot.temp_override.record_result(True, 1000.0)
        assert bot.temp_override.size == 200.0             # no ramping once retired

    def test_disabled_flag_falls_through(self, monkeypatch):
        self._defaults(monkeypatch)
        monkeypatch.setattr(bot, "TEMP_OVERRIDE_ENABLED", False)
        monkeypatch.setattr(bot, "NORMAL_TRADE_SIZE", 500.0)
        assert bot.temp_override.active is False
        assert bot.active_trade_size(1000.0) == 500.0

    def test_kelly_bet_pins_exact_size_no_ladder_up(self, monkeypatch):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        clk = _FakeClock()
        lad = StakeLadder(cfg=LadderConfig(persist=False, min_trades=10,
                                           max_multiplier=2.0, cooldown_secs=0),
                          clock=clk)
        for _ in range(12):
            lad.on_trade_result(True, 2.0)
            clk.advance(60)
        bot.stake_ladder = lad
        try:
            # Balance stays under the $5k exit so the override is still live.
            bet = bot.kelly_bet(0.65, 50, 4000.0)
            assert bet == 200.0   # override base — hot ladder cannot scale it up
        finally:
            bot.stake_ladder = None

    def test_settlement_hook_feeds_ramp(self, monkeypatch):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        bot.temp_override_record(True, 1000.0)
        bot.temp_override_record(True, 1000.0)
        assert bot.temp_override.current_size() == 210.0

    def test_persistence_round_trip(self, monkeypatch, tmp_path):
        self._no_telegram(monkeypatch); self._defaults(monkeypatch)
        path = str(tmp_path / "temp_override_state.json")
        a = bot.TempStakeOverride(path, persist=True)
        a.record_result(True, 1000.0)
        a.record_result(True, 1000.0)            # → $210, persisted
        b = bot.TempStakeOverride(path, persist=True)
        assert b.size == 210.0
        assert b.done is False
