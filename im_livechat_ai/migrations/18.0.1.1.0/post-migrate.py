# -*- coding: utf-8 -*-
"""18.0.1.1.0 post-migration.

- Drop the legacy credential columns if they are still there (values were
  already wiped by pre-migrate).
- Turn AI off on every channel: an administrator must configure LiteLLM in
  Settings > Live Chat AI and re-enable each channel.
- Remove conversation content and upstream response bodies from existing
  API logs ("Log Conversation Content" is off by default).
- Package C: drop ai_retry_delay (fixed 3 s / 10 s backoff now) and cap
  ai_max_retries to 2 retries (it used to count total calls, 1-10).
"""

import logging

from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

TABLE = 'im_livechat_channel'
LEGACY_COLUMNS = ('ai_api_key', 'ai_api_base_url')
UNUSED_COLUMNS = ('ai_retry_delay',)
MAX_RETRIES = 2
LOG_TABLE = 'llm_api_log'
MAX_LOG_ERROR_LENGTH = 300


def drop_legacy_columns(cr):
    dropped = []
    for column in LEGACY_COLUMNS:
        if sql.column_exists(cr, TABLE, column):
            # Wipe again in case anything wrote between pre and post.
            cr.execute(SQL("UPDATE %s SET %s = NULL", SQL.identifier(TABLE), SQL.identifier(column)))
            cr.execute(SQL("ALTER TABLE %s DROP COLUMN %s", SQL.identifier(TABLE), SQL.identifier(column)))
            dropped.append(column)
    return dropped


def drop_unused_columns(cr):
    dropped = []
    for column in UNUSED_COLUMNS:
        if sql.column_exists(cr, TABLE, column):
            cr.execute(SQL("ALTER TABLE %s DROP COLUMN %s", SQL.identifier(TABLE), SQL.identifier(column)))
            dropped.append(column)
    return dropped


def cap_max_retries(cr):
    if not sql.column_exists(cr, TABLE, 'ai_max_retries'):
        return 0
    cr.execute(SQL(
        "UPDATE %s SET ai_max_retries = %s WHERE ai_max_retries IS NULL OR ai_max_retries > %s",
        SQL.identifier(TABLE), MAX_RETRIES, MAX_RETRIES,
    ))
    return cr.rowcount


def disable_all_channels(cr):
    cr.execute(SQL(
        "UPDATE %s SET ai_enabled = FALSE WHERE ai_enabled IS DISTINCT FROM FALSE RETURNING id",
        SQL.identifier(TABLE),
    ))
    return [row[0] for row in cr.fetchall()]


def scrub_logs(cr):
    if not sql.table_exists(cr, LOG_TABLE):
        return 0
    cr.execute(SQL(
        """UPDATE %s
              SET request_payload = NULL,
                  response_payload = NULL,
                  error_message = LEFT(split_part(error_message, ' | Response:', 1), %s)
            WHERE request_payload IS NOT NULL
               OR response_payload IS NOT NULL
               OR error_message LIKE %s
               OR length(error_message) > %s""",
        SQL.identifier(LOG_TABLE), MAX_LOG_ERROR_LENGTH, '%| Response:%', MAX_LOG_ERROR_LENGTH,
    ))
    return cr.rowcount


def migrate(cr, version):
    if not version:
        return
    dropped = drop_legacy_columns(cr)
    if dropped:
        _logger.info("im_livechat_ai: dropped legacy columns %s.%s", TABLE, dropped)
    disabled = disable_all_channels(cr)
    _logger.warning(
        "im_livechat_ai 18.0.1.1.0: AI replies were turned OFF on all live chat channels "
        "(%s channel(s) had AI on: %s). The per-channel API URL/key were removed. "
        "A system administrator must set the LiteLLM URL and API key in Settings > Live Chat AI, "
        "click 'Fetch Available Models', then re-enable AI on each channel.",
        len(disabled), disabled,
    )
    unused = drop_unused_columns(cr)
    if unused:
        _logger.info("im_livechat_ai: dropped unused columns %s.%s", TABLE, unused)
    capped = cap_max_retries(cr)
    if capped:
        _logger.info("im_livechat_ai: capped ai_max_retries to %s on %s channel(s)", MAX_RETRIES, capped)
    scrubbed = scrub_logs(cr)
    if scrubbed:
        _logger.info("im_livechat_ai: removed conversation content/response bodies from %s API log(s)", scrubbed)
