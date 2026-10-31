# -*- coding: utf-8 -*-
"""18.0.1.1.0 pre-migration: wipe the per-channel LLM credentials.

18.0.1.0.x stored an API base URL and API key on every im_livechat.channel.
Those keys leaked (MiniMax) and the connection is now a system-level LiteLLM
setting. The values are wiped before the new code loads, so the old columns
never hold a key after the upgrade (Odoo drops the columns themselves at the
end of the upgrade, post-migrate drops any leftover).
"""

import logging

from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

TABLE = 'im_livechat_channel'
LEGACY_COLUMNS = ('ai_api_key', 'ai_api_base_url')


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


def migrate(cr, version):
    if not version:
        return
    wipe_legacy_credentials(cr)
