"""MarkeyMachine management dashboard — a thin Flask control panel that runs the
bot.py worker, selects a Trading Format, and surfaces live status.

Single-account today, multi-tenant-ready: the account store is a list and every
worker is isolated by its own state directory, so adding client accounts later
is incremental rather than a rewrite.
"""
