# -*- coding: utf-8 -*-
"""18.0.1.2.0 post-migration: the backend connection moves to the channels.

18.0.1.1.0 kept one system-wide LiteLLM connection and the log settings in
ir.config_parameter (Settings > Live Chat AI). 18.0.1.2.0 has a connection
per channel (backend type, endpoint URL, access token) and per-channel log
settings, and nothing in the Settings app. This script:

- sets ai_backend_type = 'openai' on every channel that has none (all of
  them on the first run: 1.1.0 only spoke the OpenAI protocol);
- copies im_livechat_ai.base_url (normalized: no trailing slash, no
  /chat/completions) and im_livechat_ai.api_key to every channel's
  endpoint URL and access token, where the channel has none yet. When only
  the key exists, 1.1.0 used its built-in default URL, so that URL is
  copied (the channel keeps working);
- copies im_livechat_ai.log_payload and im_livechat_ai.log_retention_days
  to every channel;
- deletes every im_livechat_ai.* system parameter (the key included);
- sets "Visitor Files" (ai_document_mode, owner decision 39) where it is
  empty (pre-migrate creates the column empty): 'link' for Hermes channels
  (model 'hermes-agent' or an endpoint URL containing 'hermes', which take
  no file content), 'file' for the others;
- clears the handoff keywords of channels that still have the 18.0.1.1.0
  default list (owner decision 40: the backend decides the handoff, the
  keywords are an optional pre-filter, empty by default); custom lists are
  kept.

ai_enabled is not touched: a channel with AI on and copied credentials keeps
answering; one with AI on but no credentials stays on and is skipped until a
system administrator fills in its connection (the AI tab says so). ai_model
keeps its value (the column was already text). Idempotent: once the
parameters are gone a re-run changes nothing, and values already on a
channel are never overwritten. Only counts are logged, never values.
"""

import logging

from odoo import SUPERUSER_ID, api
from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

TABLE = 'im_livechat_channel'
TOKEN_COLUMN = 'ai_api_token_secret'
ICP_PREFIX = 'im_livechat_ai.'
ICP_BASE_URL = 'im_livechat_ai.base_url'
ICP_API_KEY = 'im_livechat_ai.api_key'
ICP_LOG_PAYLOAD = 'im_livechat_ai.log_payload'
ICP_LOG_RETENTION_DAYS = 'im_livechat_ai.log_retention_days'
# The URL 18.0.1.1.0 used when im_livechat_ai.base_url was not set.
LEGACY_DEFAULT_BASE_URL = 'https://paas-cs-32e92be2-5b2e4e66.woowtech.io/v1'
DEFAULT_LOG_RETENTION_DAYS = 14
# The 18.0.1.1.0 default handoff keywords (cleared where unchanged).
HANDOFF_KEYWORDS_1_1_0 = (
    '找真人,真人客服,轉真人,轉人工,人工客服,找客服,聯絡客服,我要客服,'
    'human agent,talk to a human,real person,live agent'
)
MAX_LOG_RETENTION_DAYS = 3650


def normalize_base_url(url):
    url = (url or '').strip().rstrip('/')
    if url.endswith('/chat/completions'):
        url = url[:-len('/chat/completions')].rstrip('/')
    return url


def ensure_columns(cr):
    """The registry created them; added here too so the SQL below never fails."""
    for column, kind in (('ai_backend_type', 'VARCHAR'), ('ai_endpoint_url', 'VARCHAR'),
                         (TOKEN_COLUMN, 'VARCHAR'), ('ai_log_payload', 'BOOLEAN'),
                         ('ai_log_retention_days', 'INTEGER'), ('ai_document_mode', 'VARCHAR')):
        if not sql.column_exists(cr, TABLE, column):
            cr.execute(SQL("ALTER TABLE %s ADD COLUMN %s " + kind,
                           SQL.identifier(TABLE), SQL.identifier(column)))


def read_params(cr):
    cr.execute("SELECT key, value FROM ir_config_parameter WHERE key LIKE %s", (ICP_PREFIX + '%',))
    return dict(cr.fetchall())


def migrate_connection(cr, params):
    """Returns a dict of counts (never values)."""
    counts = {}
    cr.execute(SQL("UPDATE %s SET ai_backend_type = 'openai' WHERE ai_backend_type IS NULL OR ai_backend_type = ''",
                   SQL.identifier(TABLE)))
    counts['backend_type_set'] = cr.rowcount

    base_url = normalize_base_url(params.get(ICP_BASE_URL))
    api_key = (params.get(ICP_API_KEY) or '').strip()
    counts['legacy_default_url'] = 0
    if api_key and not base_url:
        base_url = LEGACY_DEFAULT_BASE_URL
        counts['legacy_default_url'] = 1
    counts['url_copied'] = counts['token_copied'] = 0
    if base_url:
        cr.execute(SQL("UPDATE %s SET ai_endpoint_url = %s WHERE ai_endpoint_url IS NULL OR ai_endpoint_url = ''",
                       SQL.identifier(TABLE), base_url))
        counts['url_copied'] = cr.rowcount
    if api_key:
        cr.execute(SQL("UPDATE %s SET %s = %s WHERE %s IS NULL OR %s = ''",
                       SQL.identifier(TABLE), SQL.identifier(TOKEN_COLUMN), api_key,
                       SQL.identifier(TOKEN_COLUMN), SQL.identifier(TOKEN_COLUMN)))
        counts['token_copied'] = cr.rowcount

    counts['log_settings_copied'] = 0
    if ICP_LOG_PAYLOAD in params:
        log_payload = str(params[ICP_LOG_PAYLOAD]).strip().lower() in ('true', '1')
        cr.execute(SQL("UPDATE %s SET ai_log_payload = %s", SQL.identifier(TABLE), log_payload))
        counts['log_settings_copied'] = cr.rowcount
    cr.execute(SQL("UPDATE %s SET ai_log_payload = FALSE WHERE ai_log_payload IS NULL", SQL.identifier(TABLE)))
    if ICP_LOG_RETENTION_DAYS in params:
        try:
            days = int(params[ICP_LOG_RETENTION_DAYS])
        except (TypeError, ValueError):
            days = DEFAULT_LOG_RETENTION_DAYS
        if days < 1:
            days = DEFAULT_LOG_RETENTION_DAYS
        cr.execute(SQL("UPDATE %s SET ai_log_retention_days = %s", SQL.identifier(TABLE),
                       min(days, MAX_LOG_RETENTION_DAYS)))
        counts['log_settings_copied'] = max(counts['log_settings_copied'], cr.rowcount)
    cr.execute(SQL("UPDATE %s SET ai_log_retention_days = %s WHERE ai_log_retention_days IS NULL "
                   "OR ai_log_retention_days < 1", SQL.identifier(TABLE), DEFAULT_LOG_RETENTION_DAYS))

    cr.execute("DELETE FROM ir_config_parameter WHERE key LIKE %s", (ICP_PREFIX + '%',))
    counts['params_deleted'] = cr.rowcount
    counts.update(migrate_document_mode(cr))
    counts['keywords_cleared'] = clear_default_handoff_keywords(cr)
    return counts


def clear_default_handoff_keywords(cr):
    """Empty the keywords that still equal the 1.1.0 default (whitespace
    ignored). Returns the number of channels changed."""
    if not sql.column_exists(cr, TABLE, 'ai_handoff_keywords'):
        return 0
    cr.execute(SQL(
        "UPDATE %s SET ai_handoff_keywords = NULL "
        "WHERE regexp_replace(ai_handoff_keywords, '\\s', '', 'g') = regexp_replace(%s, '\\s', '', 'g')",
        SQL.identifier(TABLE), HANDOFF_KEYWORDS_1_1_0))
    return cr.rowcount


def migrate_document_mode(cr):
    """Empty ai_document_mode -> 'link' (Hermes) or 'file'. Returns counts."""
    cr.execute(SQL("""
        UPDATE %s SET ai_document_mode = CASE
            WHEN lower(trim(coalesce(ai_model, ''))) = 'hermes-agent'
              OR coalesce(ai_endpoint_url, '') ILIKE %s THEN 'link'
            ELSE 'file' END
         WHERE ai_document_mode IS NULL OR ai_document_mode = ''
     RETURNING ai_document_mode""", SQL.identifier(TABLE), '%hermes%'))
    modes = [row[0] for row in cr.fetchall()]
    return {'document_mode_link': modes.count('link'), 'document_mode_file': modes.count('file')}


def channel_summary(cr):
    cr.execute(SQL("""
        SELECT count(*),
               count(*) FILTER (WHERE ai_enabled),
               count(*) FILTER (WHERE ai_enabled AND coalesce(ai_endpoint_url, '') != ''
                                AND coalesce(%s, '') != '')
          FROM %s""", SQL.identifier(TOKEN_COLUMN), SQL.identifier(TABLE)))
    total, enabled, enabled_with_connection = cr.fetchone()
    return {'channels': total, 'ai_enabled': enabled, 'ai_enabled_with_connection': enabled_with_connection}


def migrate(cr, version):
    if not version:
        return
    ensure_columns(cr)
    params = read_params(cr)
    counts = migrate_connection(cr, params)
    counts.update(channel_summary(cr))
    env = api.Environment(cr, SUPERUSER_ID, {})
    env.registry.clear_cache()  # ir.config_parameter.get_param is cached
    env['im_livechat.channel'].invalidate_model()
    _logger.warning(
        "im_livechat_ai 18.0.1.2.0: backend connection moved to the channels: %(channels)s channel(s), "
        "backend type set on %(backend_type_set)s, endpoint URL copied to %(url_copied)s "
        "(1.1.0 default URL used: %(legacy_default_url)s), access token copied to %(token_copied)s, "
        "log settings copied to %(log_settings_copied)s, %(params_deleted)s im_livechat_ai.* system "
        "parameter(s) deleted; visitor files set to link on %(document_mode_link)s and to file on "
        "%(document_mode_file)s channel(s); 1.1.0 default handoff keywords cleared on %(keywords_cleared)s "
        "channel(s); AI enabled on %(ai_enabled)s channel(s), %(ai_enabled_with_connection)s "
        "of them with URL and token", counts,
    )
