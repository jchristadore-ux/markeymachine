"""test_formats.py — Trading Format preset registry.

These tests import only formats.py (no Kalshi credentials needed).
"""

import logging

import formats
from formats import FORMATS, DEFAULT_FORMAT, apply_format, list_formats


# Env keys a format is allowed to set — every one is read by bot.py / ladder.py.
# A typo'd key would be silently ignored by the bot, so we pin the allowlist.
KNOWN_KEYS = {
    "DEMO_MODE", "NORMAL_TRADE_SIZE", "RECOVERY_TRADE_SIZE", "LADDER_ENABLED",
    "PROBATION_RAMP_ENABLED", "RECOVERY_LADDER_PAUSE_TRADES",
    "REQUIRE_AGREE_MOMENTUM", "OB_IMBALANCE_THRESH", "MIN_OB_DEPTH_DOLLARS",
    "R2_TREND_THRESHOLD", "MIN_CONFIDENCE", "MIN_EDGE_PCT", "MIN_WIN_PROB",
    "MAX_CONCURRENT_POS", "MAX_CONSEC_LOSSES", "SESSION_STOP_FRACTION",
    "YES_BREAKEVEN_PRICE",
}


def test_default_format_exists():
    assert DEFAULT_FORMAT in FORMATS


def test_expected_formats_present():
    assert set(FORMATS) == {"conservative", "balanced", "aggressive", "recovery_first"}


def test_every_preset_only_sets_known_keys():
    for name, spec in FORMATS.items():
        unknown = set(spec["settings"]) - KNOWN_KEYS
        assert not unknown, f"{name} sets unknown env keys: {unknown}"


def test_every_preset_defaults_to_paper():
    for name, spec in FORMATS.items():
        assert spec["settings"]["DEMO_MODE"] == "true", f"{name} must default to PAPER"


def test_apply_format_seeds_environment(monkeypatch):
    for key in KNOWN_KEYS:
        monkeypatch.delenv(key, raising=False)
    resolved = apply_format("conservative")
    assert resolved == "conservative"
    # A representative preset value made it into the environment.
    import os
    assert os.environ["NORMAL_TRADE_SIZE"] == "250.0"
    assert os.environ["MIN_CONFIDENCE"] == "70"


def test_explicit_env_var_wins_over_preset(monkeypatch):
    # setdefault semantics: an already-set value is never clobbered.
    monkeypatch.setenv("OB_IMBALANCE_THRESH", "0.99")
    monkeypatch.delenv("NORMAL_TRADE_SIZE", raising=False)
    apply_format("conservative")
    import os
    assert os.environ["OB_IMBALANCE_THRESH"] == "0.99"        # explicit wins
    assert os.environ["NORMAL_TRADE_SIZE"] == "250.0"          # preset fills the gap


def test_unknown_format_falls_back_to_default(monkeypatch, caplog):
    for key in KNOWN_KEYS:
        monkeypatch.delenv(key, raising=False)
    with caplog.at_level(logging.WARNING, logger="MarkeyMachine.formats"):
        resolved = apply_format("does-not-exist")
    assert resolved == DEFAULT_FORMAT
    assert any("Unknown TRADING_FORMAT" in r.message for r in caplog.records)


def test_blank_name_falls_back_silently(monkeypatch):
    resolved = apply_format("")
    assert resolved == DEFAULT_FORMAT


def test_name_normalization(monkeypatch):
    # Hyphens / spaces / case are normalized to the canonical key.
    assert apply_format("Recovery-First") == "recovery_first"
    assert apply_format("  AGGRESSIVE ") == "aggressive"


def test_list_formats_shape():
    items = list_formats()
    assert len(items) == len(FORMATS)
    for it in items:
        assert {"name", "display_name", "blurb", "description", "settings", "is_default"} <= set(it)
    assert sum(1 for it in items if it["is_default"]) == 1
