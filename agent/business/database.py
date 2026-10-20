"""Runtime-selected facade for the foreign-trade database backends.

The backend is resolved for every call instead of once at import time. This
keeps long-lived tool functions correct after configuration reloads and makes
isolated SQLite tests independent from a process that normally runs on MySQL.
"""

from __future__ import annotations

from functools import wraps

from agent.business.config import load_business_config


def _backend():
    if load_business_config().database_backend == "mysql":
        from agent.business import mysql_database

        return mysql_database
    from agent.business import sqlite_database

    return sqlite_database


def _dispatch(name):
    sqlite_backend = __import__("agent.business.sqlite_database", fromlist=[name])

    @wraps(getattr(sqlite_backend, name))
    def call(*args, **kwargs):
        return getattr(_backend(), name)(*args, **kwargs)

    return call


get_connection = _dispatch("get_connection")
tx = _dispatch("tx")
close_all = _dispatch("close_all")
init_db = _dispatch("init_db")
list_products = _dispatch("list_products")
get_product_by_sku = _dispatch("get_product_by_sku")
get_product_by_id = _dispatch("get_product_by_id")
check_inventory = _dispatch("check_inventory")
create_rfq = _dispatch("create_rfq")
update_rfq_extraction = _dispatch("update_rfq_extraction")
get_rfq = _dispatch("get_rfq")
create_quote = _dispatch("create_quote")
add_quote_version = _dispatch("add_quote_version")
update_quote_status = _dispatch("update_quote_status")
get_quote = _dispatch("get_quote")
list_quotes_by_session = _dispatch("list_quotes_by_session")
create_followup = _dispatch("create_followup")
list_pending_followups = _dispatch("list_pending_followups")
create_approval = _dispatch("create_approval")
approve = _dispatch("approve")
upsert_exchange_rate = _dispatch("upsert_exchange_rate")
get_exchange_rate = _dispatch("get_exchange_rate")


__all__ = [name for name in globals() if not name.startswith("_")]
