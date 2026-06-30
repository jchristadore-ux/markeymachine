"""
formats.py — Trading Formats (named presets) for MarkeyMachine.

WHY
---
Across its history MarkeyMachine never changed its *strategy* (short-dated,
trend-confirming order-book pressure). What changed — repeatedly, by owner
directive and log review — was the **sizing policy and the risk/gate
thresholds**: Kelly-scaled → flat stake → two-tier Recovery → Probation ramp,
with the Ladder overlay on or off and the entry gates tightened or relaxed.

All of that is already steered by ~40 individual environment variables read in
`bot.py`. A "Trading Format" is simply a **named bundle of those env values** so
the whole posture can be switched with one knob:

    TRADING_FORMAT=conservative python bot.py

DESIGN
------
`apply_format()` seeds each preset value into the environment with
`os.environ.setdefault()` — it only fills a key that is *not already set*. So:

  • Selecting a format gives you its whole posture in one switch.
  • Any explicit env var (Railway config, a one-off override, the dashboard)
    still wins over the preset — formats set defaults, they never clobber.

It must run BEFORE `bot.py` reads its config block (the module-level
`_env_*()` calls), so `bot.py` imports and calls this right after defining its
env helpers and before the first `_require()`.

This module is intentionally dependency-free and does NOT import `bot.py`, so it
can be listed (`python formats.py`) and unit-tested without Kalshi credentials.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Dict, List

log = logging.getLogger("MarkeyMachine.formats")

DEFAULT_FORMAT = "balanced"

# Keys every preset sets, so a format fully defines the posture rather than
# inheriting stray values from a previously-selected one. (Documentation aid;
# apply_format simply iterates each preset's own "settings".)
#
# NOTE ON SIZES: NORMAL_TRADE_SIZE / RECOVERY_TRADE_SIZE below are sensible
# DEFAULTS tuned for a ~$1–2k bankroll and matching the documented production
# baseline ($500 normal / $100 recovery for "balanced"). They are seeded with
# setdefault, so set NORMAL_TRADE_SIZE / RECOVERY_TRADE_SIZE explicitly (env or
# dashboard) to size to your own account — the format will not override you.
# The bot already clamps any stake to cash on hand, so an oversized default is
# safe on a small (e.g. paper $25) balance.

FORMATS: Dict[str, dict] = {
    "conservative": {
        "display_name": "Conservative — Capital Preservation",
        "blurb": "Fewer, higher-conviction trades. Strict gates, recovery + "
                 "probation on, ladder off. Built to protect the bankroll.",
        "description": (
            "Tightens every entry gate (order-book imbalance, regime R², "
            "confidence, edge, win-prob), requires BTC momentum AGREE, runs one "
            "position at a time, and keeps the graduated Recovery/Probation "
            "sizing on with the Ladder overlay off. Smaller base stake and an "
            "earlier session-stop. Lowest trade frequency, lowest variance."
        ),
        "settings": {
            "DEMO_MODE": "true",
            "NORMAL_TRADE_SIZE": 250.0,
            "RECOVERY_TRADE_SIZE": 50.0,
            "LADDER_ENABLED": "false",
            "PROBATION_RAMP_ENABLED": "true",
            "REQUIRE_AGREE_MOMENTUM": "true",
            "OB_IMBALANCE_THRESH": 0.75,
            "MIN_OB_DEPTH_DOLLARS": 100.0,
            "R2_TREND_THRESHOLD": 0.70,
            "MIN_CONFIDENCE": 70,
            "MIN_EDGE_PCT": 0.08,
            "MIN_WIN_PROB": 0.62,
            "MAX_CONCURRENT_POS": 1,
            "MAX_CONSEC_LOSSES": 2,
            "SESSION_STOP_FRACTION": 0.60,
            "YES_BREAKEVEN_PRICE": 65,
        },
    },
    "balanced": {
        "display_name": "Balanced — Standard (v9.6 defaults)",
        "blurb": "The shipped production posture. Two-tier Recovery + Probation "
                 "sizing, ladder off, gates at doctrine defaults.",
        "description": (
            "The current v9.6 production configuration: $500 normal / $100 "
            "recovery sizing with the Probation ramp on and the Ladder overlay "
            "off, momentum-AGREE required, and the doctrine entry thresholds "
            "(OB imbalance 0.70, R² 0.65, confidence 65, edge 6%, win-prob 60%). "
            "A sensible middle ground."
        ),
        "settings": {
            "DEMO_MODE": "true",
            "NORMAL_TRADE_SIZE": 500.0,
            "RECOVERY_TRADE_SIZE": 100.0,
            "LADDER_ENABLED": "false",
            "PROBATION_RAMP_ENABLED": "true",
            "REQUIRE_AGREE_MOMENTUM": "true",
            "OB_IMBALANCE_THRESH": 0.70,
            "MIN_OB_DEPTH_DOLLARS": 75.0,
            "R2_TREND_THRESHOLD": 0.65,
            "MIN_CONFIDENCE": 65,
            "MIN_EDGE_PCT": 0.06,
            "MIN_WIN_PROB": 0.60,
            "MAX_CONCURRENT_POS": 1,
            "MAX_CONSEC_LOSSES": 2,
            "SESSION_STOP_FRACTION": 0.40,
            "YES_BREAKEVEN_PRICE": 67,
        },
    },
    "aggressive": {
        "display_name": "Aggressive — Edge Hunter",
        "blurb": "More trades, bigger stake, Ladder overlay on (up to 2×). "
                 "Higher throughput and higher variance.",
        "description": (
            "Relaxes the entry gates (lower OB imbalance, R², confidence, edge "
            "and win-prob floors), allows two concurrent positions, raises the "
            "loss-streak pause threshold, and turns the performance-driven "
            "Ladder overlay ON so a hot win rate scales the stake up to 2×. "
            "Larger base stake. Highest trade frequency and variance — only the "
            "always-on guardrails (streak pause, session stop, ladder drawdown "
            "caps) remain."
        ),
        "settings": {
            "DEMO_MODE": "true",
            "NORMAL_TRADE_SIZE": 750.0,
            "RECOVERY_TRADE_SIZE": 150.0,
            "LADDER_ENABLED": "true",
            "PROBATION_RAMP_ENABLED": "true",
            "REQUIRE_AGREE_MOMENTUM": "true",
            "OB_IMBALANCE_THRESH": 0.65,
            "MIN_OB_DEPTH_DOLLARS": 50.0,
            "R2_TREND_THRESHOLD": 0.60,
            "MIN_CONFIDENCE": 60,
            "MIN_EDGE_PCT": 0.05,
            "MIN_WIN_PROB": 0.58,
            "MAX_CONCURRENT_POS": 2,
            "MAX_CONSEC_LOSSES": 3,
            "SESSION_STOP_FRACTION": 0.35,
            "YES_BREAKEVEN_PRICE": 70,
        },
    },
    "recovery_first": {
        "display_name": "Recovery-First — Drawdown Guard",
        "blurb": "Smallest stake, strict gates, recovery + probation emphasized, "
                 "ladder off. Designed to claw back and stay small.",
        "description": (
            "A drawdown-defensive posture: small base stake, strict entry gates, "
            "Ladder off, and the Recovery/Probation graduated re-entry "
            "emphasized — with a longer post-recovery ladder pause so the edge "
            "must re-prove itself on fresh data before any size-up. One position "
            "at a time and an early session-stop. Best for rebuilding after a "
            "rough stretch."
        ),
        "settings": {
            "DEMO_MODE": "true",
            "NORMAL_TRADE_SIZE": 200.0,
            "RECOVERY_TRADE_SIZE": 50.0,
            "LADDER_ENABLED": "false",
            "PROBATION_RAMP_ENABLED": "true",
            "RECOVERY_LADDER_PAUSE_TRADES": 8,
            "REQUIRE_AGREE_MOMENTUM": "true",
            "OB_IMBALANCE_THRESH": 0.72,
            "MIN_OB_DEPTH_DOLLARS": 100.0,
            "R2_TREND_THRESHOLD": 0.68,
            "MIN_CONFIDENCE": 68,
            "MIN_EDGE_PCT": 0.07,
            "MIN_WIN_PROB": 0.61,
            "MAX_CONCURRENT_POS": 1,
            "MAX_CONSEC_LOSSES": 2,
            "SESSION_STOP_FRACTION": 0.60,
            "YES_BREAKEVEN_PRICE": 65,
        },
    },
}


def _resolve(name: str) -> str:
    """Normalize a requested format name to a known key, or DEFAULT_FORMAT."""
    key = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in FORMATS:
        return key
    if key:
        log.warning("Unknown TRADING_FORMAT %r — falling back to %r. Known: %s",
                    name, DEFAULT_FORMAT, ", ".join(FORMATS))
    return DEFAULT_FORMAT


def apply_format(name: str) -> str:
    """Seed the selected format's settings into os.environ as DEFAULTS.

    Uses os.environ.setdefault, so a value already present in the environment
    (Railway config, an explicit override, the dashboard) is never clobbered —
    formats define the posture, explicit env vars win. Returns the resolved
    format name actually applied.
    """
    resolved = _resolve(name)
    for key, value in FORMATS[resolved]["settings"].items():
        os.environ.setdefault(key, str(value))
    log.info("Trading format: %s (%s)", resolved, FORMATS[resolved]["display_name"])
    return resolved


def list_formats() -> List[dict]:
    """Format metadata for the dashboard / CLI (no side effects)."""
    return [
        {
            "name": name,
            "display_name": spec["display_name"],
            "blurb": spec["blurb"],
            "description": spec["description"],
            "settings": dict(spec["settings"]),
            "is_default": name == DEFAULT_FORMAT,
        }
        for name, spec in FORMATS.items()
    ]


def print_formats() -> None:
    """Human-readable listing for `python formats.py` / `bot.py --list-formats`."""
    print("MarkeyMachine — Trading Formats\n")
    for spec in list_formats():
        star = "  (default)" if spec["is_default"] else ""
        print(f"  {spec['name']}{star}")
        print(f"      {spec['display_name']}")
        print(f"      {spec['blurb']}")
        size = spec["settings"].get("NORMAL_TRADE_SIZE")
        ladder = spec["settings"].get("LADDER_ENABLED")
        print(f"      base=${size} ladder={ladder} "
              f"OB≥{spec['settings'].get('OB_IMBALANCE_THRESH')} "
              f"R²≥{spec['settings'].get('R2_TREND_THRESHOLD')}\n")
    print("Select with:  TRADING_FORMAT=<name> python bot.py")
    print("Explicit env vars always override a format's defaults.")


if __name__ == "__main__":
    if "--json" in sys.argv:
        import json
        print(json.dumps(list_formats(), indent=2))
    else:
        print_formats()
