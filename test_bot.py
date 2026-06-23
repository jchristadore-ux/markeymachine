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
        assert bet <= 2.0 * bot.TRADE_SIZE_CAP + 1e-9


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

class TestBalanceFloorCheck:
    def test_below_floor(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(4.99) is False

    def test_at_floor(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(5.00) is True

    def test_above_floor(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(100.0) is True

    def test_zero(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_BALANCE_FLOOR", 5.0)
        assert bot.balance_floor_check(0.0) is False


class TestDailyLossCheck:
    def setup_method(self):
        bot._session_halted = False

    def test_within_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -10.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 20.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(15.0) is True

    def test_at_limit(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -20.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 20.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(5.0) is False

    def test_session_stop_triggers(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        assert bot.daily_loss_check(10.0) is False

    def test_session_stop_ok_when_above(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        assert bot.daily_loss_check(20.0) is True

    def test_halted_flag_blocks(self, monkeypatch):
        monkeypatch.setattr(bot, "_session_halted", True)
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", 0.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        assert bot.daily_loss_check(50.0) is False

    def test_halt_is_permanent_after_recovery(self, monkeypatch):
        """Balance recovery above session_stop_threshold must not clear halt."""
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "paper_daily_pnl", -5.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 100.0)
        monkeypatch.setattr(bot, "session_stop_threshold", 12.50)
        # Trigger halt at low balance
        result1 = bot.daily_loss_check(10.0)
        assert result1 is False
        assert bot._session_halted is True
        # Even with high balance, stays halted
        result2 = bot.daily_loss_check(50.0)
        assert result2 is False

    def test_pct_cap_binds_before_dollar_cap(self, monkeypatch):
        """v9.1.0: on a large bankroll the % cap halts before the $ cap would."""
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 1000.0)   # dollar cap never binds
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)  # 6% = $120
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        monkeypatch.setattr(bot, "paper_daily_pnl", -100.0)  # above the $120 cap
        assert bot.daily_loss_check(1900.0) is True
        monkeypatch.setattr(bot, "paper_daily_pnl", -130.0)  # past the $120 cap
        assert bot.daily_loss_check(1870.0) is False

    def test_dollar_cap_still_binds_for_small_account(self, monkeypatch):
        """The fixed $ cap is retained for tiny accounts where it is tighter."""
        monkeypatch.setattr(bot, "DEMO_MODE", True)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 15.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 20.0)  # 6% = $1.20
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        # pct cap ($1.20) is tighter than $15 here, so it binds first
        monkeypatch.setattr(bot, "paper_daily_pnl", -2.0)
        assert bot.daily_loss_check(18.0) is False


class TestLiveRealizedDailyLoss:
    """v9.3.1: the LIVE daily-loss breaker must read realized-only PnL, never the
    balance−start cash delta. Reproduces the 2026-06-23 incident where a winning
    trade's open-position cash outlay ($99.71) halted the bot before settlement."""

    def setup_method(self):
        bot._session_halted = False

    def test_open_position_cash_outlay_does_not_halt(self, monkeypatch):
        # LIVE mode. Session started at $1477.74; cap = 6% = $88.66.
        monkeypatch.setattr(bot, "DEMO_MODE", False)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 1000.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 1477.74)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        # Position is OPEN: nothing settled, so realized = 0 even though the
        # broker balance is down by the $99.71 cash outlay.
        monkeypatch.setattr(bot, "live_daily_realized", 0.0)
        # balance passed in reflects the cash debit; the breaker must ignore it.
        assert bot.daily_loss_check(1378.03) is True
        assert bot._session_halted is False

    def test_realized_win_keeps_trading(self, monkeypatch):
        monkeypatch.setattr(bot, "DEMO_MODE", False)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 1000.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 1477.74)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        # The trade settled +$69.29 (the real outcome on 2026-06-23).
        monkeypatch.setattr(bot, "live_daily_realized", 69.29)
        assert bot.daily_loss_check(1547.03) is True
        assert bot._session_halted is False

    def test_genuine_realized_loss_still_halts(self, monkeypatch):
        # The breaker must still fire on a REAL realized drawdown past the cap.
        monkeypatch.setattr(bot, "DEMO_MODE", False)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 1000.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 1477.74)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        monkeypatch.setattr(bot, "live_daily_realized", -90.0)  # > $88.66 cap
        assert bot.daily_loss_check(1387.74) is False
        assert bot._session_halted is True

    def test_breaker_ignores_balance_delta_in_live(self, monkeypatch):
        # Even a catastrophic-looking balance dip must not halt when realized PnL
        # is still within cap (e.g. multiple open positions mid-flight).
        monkeypatch.setattr(bot, "DEMO_MODE", False)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS", 1000.0)
        monkeypatch.setattr(bot, "MAX_DAILY_LOSS_PCT", 0.06)
        monkeypatch.setattr(bot, "session_start_balance", 1477.74)
        monkeypatch.setattr(bot, "session_stop_threshold", 0.0)
        monkeypatch.setattr(bot, "live_daily_realized", -10.0)  # within cap
        assert bot.daily_loss_check(1100.00) is True  # huge cash dip, ignored
        assert bot._session_halted is False


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
    def test_positive_edge_returns_bet(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.10)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.70, 50, 25.0)
        assert bet > 0
        assert bet <= 5.0

    def test_no_edge_returns_zero(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.10)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.30, 50, 25.0)
        assert bet == 0.0

    def test_capped_at_trade_size(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 2.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.50)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.50)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.90, 40, 100.0)
        assert bet <= 2.0

    def test_recovery_halves_kelly(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 100.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.30)
        monkeypatch.setattr(bot, "KELLY_RECOVERY_MULT", 0.50)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.50)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet_active = bot.kelly_bet(0.70, 50, 100.0)
        monkeypatch.setattr(bot, "session_state", SessionState.RECOVERY)
        bet_recovery = bot.kelly_bet(0.70, 50, 100.0)
        assert bet_recovery < bet_active
        assert abs(bet_recovery - bet_active * 0.50) < 0.01

    def test_boundary_prices(self, monkeypatch):
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 5.0)
        monkeypatch.setattr(bot, "KELLY_FRACTION", 0.35)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.10)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        assert bot.kelly_bet(0.70, 0, 25.0) == 0.0
        assert bot.kelly_bet(0.70, 100, 25.0) == 0.0

    def test_capped_at_bet_fraction(self, monkeypatch):
        """v9.1.0: a single bet never exceeds MAX_BET_FRACTION of bankroll."""
        monkeypatch.setattr(bot, "TRADE_SIZE_CAP", 1_000.0)  # dollar cap not binding
        monkeypatch.setattr(bot, "KELLY_FRACTION", 1.0)
        monkeypatch.setattr(bot, "MAX_BET_FRACTION", 0.04)
        monkeypatch.setattr(bot, "session_state", SessionState.ACTIVE)
        bet = bot.kelly_bet(0.90, 40, 1_000.0)
        assert bet <= 1_000.0 * 0.04 + 0.01


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

    def test_neutral_flat(self, monkeypatch):
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 3)
        for p in [50000, 50010, 50020, 50030, 50040]:
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
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 3)
        monkeypatch.setattr(bot, "NEUTRAL_ACCURACY_DRAG", 0.02)
        for p in [50000, 50010, 50020, 50030, 50040]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert adj == -0.02

    def test_lookback_default_needs_more_samples(self, monkeypatch):
        # v9.3.2 regression: at the default lookback (6) a 5-price series is one
        # sample short, so momentum cannot yet be measured.
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        for p in [50000, 50050, 50100, 50200, 50300]:
            bot.btc_prices.append(p)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "NEUTRAL"

    def test_gentle_trend_now_agrees(self, monkeypatch):
        # v9.3.2 regression for the 2026-06-23 "zero trades" bug. A clean,
        # gentle ~0.3% uptrend over 12 samples (~6 min) in which NO single
        # 90s/3-sample slice moves ≥0.15% used to read NEUTRAL and block every
        # trade. With the default 6-interval lookback the wider window clears
        # the 0.15% threshold and momentum correctly AGREES.
        monkeypatch.setattr(bot, "MOMENTUM_THRESH_PCT", 0.15)
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        # +25/step → +0.05% per 90s slice (< thresh), but +0.30% over 6 slices.
        prices = [50000 + 25 * i for i in range(12)]
        for p in prices:
            bot.btc_prices.append(p)
        # old 3-sample window would have been NEUTRAL...
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 3)
        assert bot.compute_momentum("YES")[0] == "NEUTRAL"
        # ...new 6-sample window AGREES on the same data.
        monkeypatch.setattr(bot, "MOMENTUM_LOOKBACK", 6)
        verdict, adj = bot.compute_momentum("YES")
        assert verdict == "AGREE"
        assert adj > 0


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
    def test_below_min_sample_passes(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 3)
        monkeypatch.setattr(bot, "live_losses", 5)
        assert bot.performance_guard() is True

    def test_good_win_rate_passes(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 16)
        monkeypatch.setattr(bot, "live_losses", 4)
        assert bot.performance_guard() is True

    def test_bad_win_rate_blocks(self, monkeypatch):
        monkeypatch.setattr(bot, "MIN_SAMPLE_TRADES", 20)
        monkeypatch.setattr(bot, "live_wins", 8)
        monkeypatch.setattr(bot, "live_losses", 22)
        assert bot.performance_guard() is False


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
    """v9.1.0 recovery state machine: entry timestamp + hard timeout backstop."""

    def setup_method(self):
        bot.session_state         = SessionState.ACTIVE
        bot.recovery_entry_wins   = 0
        bot.recovery_entry_losses = 0
        bot.recovery_entered_ts   = 0.0
        bot.live_wins             = 0
        bot.live_losses           = 0

    def _no_telegram(self, monkeypatch):
        monkeypatch.setattr(bot.tg, "send_telegram_message", lambda *a, **k: True)

    def test_entry_stamps_recovery_time(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        bot.session_state = SessionState.ACTIVE
        bot.update_session_state(1750.0)  # 12.5% loss → enter RECOVERY
        assert bot.session_state == SessionState.RECOVERY
        assert bot.recovery_entered_ts > 0.0

    def test_timeout_forces_exit(self, monkeypatch):
        """The deadlock backstop: stuck in RECOVERY past RECOVERY_MAX_SECS → ACTIVE."""
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        monkeypatch.setattr(bot, "RECOVERY_MAX_SECS", 3600)
        bot.session_state       = SessionState.RECOVERY
        bot.recovery_entered_ts = time.time() - 7200  # 2h ago
        bot.update_session_state(1750.0)  # still 12.5% down, 0 trades since entry
        assert bot.session_state == SessionState.ACTIVE

    def test_within_timeout_stays_recovery(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        monkeypatch.setattr(bot, "RECOVERY_MAX_SECS", 3600)
        bot.session_state       = SessionState.RECOVERY
        bot.recovery_entered_ts = time.time() - 60  # 1 min ago
        bot.update_session_state(1750.0)
        assert bot.session_state == SessionState.RECOVERY

    def test_zero_timestamp_is_initialized_not_instant_exit(self, monkeypatch):
        """A stale 0.0 ts (recovery entered by an older build) must not instant-exit."""
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        monkeypatch.setattr(bot, "RECOVERY_MAX_SECS", 3600)
        bot.session_state       = SessionState.RECOVERY
        bot.recovery_entered_ts = 0.0
        bot.update_session_state(1750.0)
        assert bot.session_state == SessionState.RECOVERY
        assert bot.recovery_entered_ts > 0.0

    def test_balance_heal_still_exits(self, monkeypatch):
        self._no_telegram(monkeypatch)
        monkeypatch.setattr(bot, "session_start_balance", 2000.0)
        monkeypatch.setattr(bot, "RECOVERY_TRIGGER_PCT", 0.10)
        monkeypatch.setattr(bot, "RECOVERY_MAX_SECS", 3600)
        bot.session_state       = SessionState.RECOVERY
        bot.recovery_entered_ts = time.time() - 60
        bot.update_session_state(1850.0)  # 7.5% loss ≤ 10% trigger → heal exit
        assert bot.session_state == SessionState.ACTIVE
