"""Shared pytest fixtures for the MarkeyMachine suite.

The TEMPORARY hard stake override (owner directive — see bot.TempStakeOverride)
ships ENABLED by default, where it preempts every other sizing mode. The vast
majority of the suite exercises the underlying recovery → probation → normal
ladder, so this autouse fixture retires the override around each test. Tests
that target the override itself re-arm it explicitly in their own setup.
"""

import pytest


@pytest.fixture(autouse=True)
def _retire_temp_override():
    # Imported lazily: test modules set the required KALSHI env/PEM before they
    # import `bot`, and that import has already happened by the time any test
    # (and therefore this fixture) runs.
    import bot

    prev_done    = bot.temp_override.done
    prev_persist = bot.temp_override._persist
    bot.temp_override._persist = False        # never touch disk during tests
    bot.temp_override.done     = True         # retired → fall through to the ladder
    try:
        yield
    finally:
        bot.temp_override.done     = prev_done
        bot.temp_override._persist = prev_persist
