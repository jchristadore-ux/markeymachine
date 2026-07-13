"""Shared pytest fixtures for the MarkeyMachine suite.

The TEMPORARY hard stake override (owner directive — see bot.TempStakeOverride)
ships ENABLED by default, where it preempts every other sizing mode. The vast
majority of the suite exercises the underlying recovery → probation → normal
ladder, so this autouse fixture retires the override around each test. Tests
that target the override itself re-arm it explicitly in their own setup.
"""

import os

import pytest

# The autouse fixture below imports `bot`, whose module-level config calls
# _require("KALSHI_API_KEY_ID") and loads the PEM at import. Seed throwaway
# credentials here — at conftest import, before any test module is collected —
# so every test file (including the dashboard/formats suites that never touch
# bot directly) can run on its own, not only when test_bot.py happens to import
# first. Real env values, if present, are respected (setdefault).
os.environ.setdefault("KALSHI_API_KEY_ID", "test-key-id-00000000")
os.environ.setdefault("DEMO_MODE", "true")
if not os.environ.get("KALSHI_PRIVATE_KEY_PEM"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    _k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    os.environ["KALSHI_PRIVATE_KEY_PEM"] = _k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def _retire_temp_override():
    # Imported lazily: test modules set the required KALSHI env/PEM before they
    # import `bot`, and that import has already happened by the time any test
    # (and therefore this fixture) runs.
    import bot

    prev_done    = bot.temp_override.done
    prev_persist = bot.temp_override._persist
    prev_pl_persist = bot.PROFIT_LOCK_PERSIST
    bot.temp_override._persist = False        # never touch disk during tests
    bot.temp_override.done     = True         # retired → fall through to the ladder
    bot.PROFIT_LOCK_PERSIST    = False        # profit lock: never touch disk either
    try:
        yield
    finally:
        bot.temp_override.done     = prev_done
        bot.temp_override._persist = prev_persist
        bot.PROFIT_LOCK_PERSIST    = prev_pl_persist
