# -*- coding: utf-8 -*-
"""18.0.1.1.0 pre-migration: wipe the per-channel LLM credentials.

18.0.1.0.x stored an API base URL and API key on every im_livechat.channel.
Those keys leaked (MiniMax) and the connection is now a system-level LiteLLM
setting. The values are wiped before the new code loads, so the old columns
never hold a key after the upgrade (Odoo drops the columns themselves at the
end of the upgrade, post-migrate drops any leftover).

It also records whether this database comes from 18.0.1.0.x (one of the
legacy columns ai_api_key, ai_api_base_url or ai_retry_delay exists) in the
system parameter im_livechat_ai.migration_1_1_0_legacy. post-migrate reads
and deletes it and only then turns AI off, converts ai_max_retries and
scrubs the API logs, so running these scripts again on an already migrated
database changes nothing (P2-4).
"""

import logging

from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

TABLE = 'im_livechat_channel'
LEGACY_COLUMNS = ('ai_api_key', 'ai_api_base_url')
# Columns only 18.0.1.0.x has: their presence marks a database to convert.
LEGACY_MARKER_COLUMNS = ('ai_api_key', 'ai_api_base_url', 'ai_retry_delay')
LEGACY_FLAG_KEY = 'im_livechat_ai.migration_1_1_0_legacy'


def wipe_legacy_credentials(cr):
    """NULL the legacy credential columns. Returns {column: rows wiped}."""
    wiped = {}
    for column in LEGACY_COLUMNS:
        if not sql.column_exists(cr, TABLE, column):
            continue
        cr.execute(SQL(
            "UPDATE %s SET %s = NULL WHERE %s IS NOT NULL",
            SQL.identifier(TABLE), SQL.identifier(column), SQL.identifier(column),
        ))
        wiped[column] = cr.rowcount
        _logger.info("im_livechat_ai: wiped %s value(s) of %s.%s", cr.rowcount, TABLE, column)
    return wiped


def is_legacy_schema(cr):
    """True when one of the 18.0.1.0.x columns still exists."""
    return any(sql.column_exists(cr, TABLE, column) for column in LEGACY_MARKER_COLUMNS)


def set_legacy_flag(cr, legacy):
    """Store (or clear) the flag read by post-migrate, with plain SQL (the
    registry is not loaded yet)."""
    cr.execute("DELETE FROM ir_config_parameter WHERE key = %s", (LEGACY_FLAG_KEY,))
    if legacy:
        cr.execute(
            """INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
               VALUES (%s, '1', 1, 1, now() at time zone 'UTC', now() at time zone 'UTC')""",
            (LEGACY_FLAG_KEY,),
        )


def migrate(cr, version):
    if not version:
        return
    legacy = is_legacy_schema(cr)
    set_legacy_flag(cr, legacy)
    _logger.info("im_livechat_ai 18.0.1.1.0 pre-migrate: legacy 18.0.1.0.x schema: %s", legacy)
    wipe_legacy_credentials(cr)
