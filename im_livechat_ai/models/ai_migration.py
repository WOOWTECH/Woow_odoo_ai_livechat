# -*- coding: utf-8 -*-
"""18.0.1.x -> 18.0.2.0.0: the backend settings of the Live Chat channels
become AI connections and AI assistants.

Up to 18.0.1.3.5 every setting was a column of ``im_livechat_channel``
(backend type, URL, the key in the SQL-only column ``ai_api_token_secret``,
model, documents, timeout, retries, content logging, prompt, history, error
message, bot partner and name). The upgrade (migrations/18.0.2.0.0
post-migrate) gives every channel that had an AI set up its own AI assistant,
and one AI connection per distinct backend setting:

- the values follow the 18.0.1.3.5 runtime rules (backend, URL normalization,
  model, document mode, timeout, retries, history);
- two channels share a connection when every setting and the key are equal;
  the key is copied and compared in SQL only (whitespace removed as 18.0.1.x
  did when sending it): it never enters Python, a log, a description, a note
  or an exception;
- each assistant reuses the channel's bot partner (when it is a bot: no user,
  not the default bot, not already another assistant's) and is available in
  Live Chat;
- channels are linked in SQL (no constraint runs, ``ai_enabled`` is kept);
- content logging is off on the new connections, and the content stored for
  channels that had it on is erased (18.0.1.x's own rule when logging is
  turned off);
- the old call logs are linked to the assistant and the connection.

Idempotent: linked channels are skipped, connections created by an earlier
run are reused, and only rows without a link are touched; a second run logs
zeros. Only counts and ids are logged, never names, URLs, prompts or keys.

The old columns stay in ``im_livechat_channel`` for one version (the
pre-migrate keeps their reflections out of the end-of-upgrade cleanup), so a
rollback to 18.0.1.3.5 keeps working. A later version drops them on purpose.
"""

import logging

from odoo import api, models
from odoo.tools import sql

from ..tools.backend import BACKEND_N8N, BACKEND_OPENAI, normalize_openai_url
from .ai_assistant import DEFAULT_BOT_XMLID, bot_avatar
from .ai_connection import (
    LITELLM_DEFAULT_TIMEOUT,
    MAX_MAX_RETRIES,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    N8N_DEFAULT_TIMEOUT,
    NO_IN_USE_CHECK,
)

_logger = logging.getLogger(__name__)

TABLE = 'im_livechat_channel'
TOKEN_COLUMN = 'ai_api_token_secret'
# 18.0.1.x channel columns read by the migration (a missing column reads as NULL)
LEGACY_COLUMNS = ('ai_backend_type', 'ai_endpoint_url', 'ai_model', 'ai_document_mode', 'ai_system_prompt',
                  'ai_max_history', 'ai_max_retries', 'ai_request_timeout', 'ai_error_message',
                  'ai_bot_partner_id', 'ai_bot_name', 'ai_log_payload')
# the key as 18.0.1.x sent it (Python's str.strip), in SQL
NORMALIZED_KEY_SQL = r"NULLIF(regexp_replace(ch.ai_api_token_secret, '^\s+|\s+$', '', 'g'), '')"


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_legacy_row(row):
    """The connection and assistant values of one 18.0.1.x channel row, with
    the 18.0.1.3.5 runtime rules (never the key)."""
    backend = row.get('ai_backend_type') if row.get('ai_backend_type') in (BACKEND_OPENAI, BACKEND_N8N) \
        else BACKEND_OPENAI
    url = (row.get('ai_endpoint_url') or '').strip()
    if backend == BACKEND_OPENAI:
        url = normalize_openai_url(url)
    model = (row.get('ai_model') or '').strip() if backend == BACKEND_OPENAI else ''
    document_mode = row.get('ai_document_mode') if row.get('ai_document_mode') in ('file', 'link') else 'file'
    timeout = _int(row.get('ai_request_timeout'))
    if timeout <= 0:
        timeout = N8N_DEFAULT_TIMEOUT if backend == BACKEND_N8N else LITELLM_DEFAULT_TIMEOUT
    timeout = max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))
    retries = max(0, min(_int(row.get('ai_max_retries')), MAX_MAX_RETRIES))
    history = _int(row.get('ai_max_history')) or 50
    history = max(1, min(history, 200))
    return {
        'backend_type': backend,
        'endpoint_url': url or None,
        'model': model or None,
        'document_mode': document_mode,
        'timeout_seconds': timeout,
        'max_retries': retries,
        'max_history': history,
    }


CONNECTION_KEY = ('backend_type', 'endpoint_url', 'model', 'document_mode', 'timeout_seconds', 'max_retries')


class LivechatAiConnectionMigration(models.Model):
    _inherit = 'im_livechat.ai.connection'

    @api.model
    def _legacy_channel_columns(self):
        """The 18.0.1.x channel columns still present (the key column apart)."""
        cr = self.env.cr
        return [column for column in LEGACY_COLUMNS if sql.column_exists(cr, TABLE, column)]

    @api.model
    def _migration_lang(self):
        """Language of the names given to the new records: the administrator's."""
        admin = self.env.ref('base.user_admin', raise_if_not_found=False)
        return self.env['im_livechat.ai.assistant']._installed_lang(admin.lang if admin else None)

    @api.model
    def _migrate_legacy_channels(self):
        """Give every 18.0.1.x channel with an AI set up its AI assistant and AI
        connection (see the module docstring).

        Returns:
            dict: counts and ids (never a name, URL, prompt or key)
        """
        cr = self.env.cr
        stats = {'channels': [], 'connections_created': [], 'connections_reused': 0, 'assistants': [],
                 'partners_reused': 0, 'partners_created': 0, 'logs_erased': 0, 'logs_linked': 0,
                 'partners_tagged': 0, 'blocked': []}
        if not sql.table_exists(cr, TABLE) or not sql.column_exists(cr, TABLE, 'ai_assistant_id'):
            return stats
        columns = self._legacy_channel_columns()
        has_token = sql.column_exists(cr, TABLE, TOKEN_COLUMN)
        self.env.flush_all()
        select = ', '.join(column if column in columns else 'NULL AS %s' % column for column in LEGACY_COLUMNS)
        url_set = "COALESCE(btrim(ai_endpoint_url), '') <> ''" if 'ai_endpoint_url' in columns else 'FALSE'
        prompt_set = "COALESCE(btrim(ai_system_prompt), '') <> ''" if 'ai_system_prompt' in columns else 'FALSE'
        cr.execute("SELECT id, name, ai_enabled, %s FROM im_livechat_channel "  # noqa: S608 - fixed column names
                   "WHERE ai_assistant_id IS NULL AND (ai_enabled OR %s OR %s) ORDER BY id"
                   % (select, url_set, prompt_set))
        rows = cr.dictfetchall()

        # stored content of the channels not migrated yet, whose logging was on (Q13 ⑥)
        if 'ai_log_payload' in columns and sql.table_exists(cr, 'llm_api_log'):
            cr.execute("""
                UPDATE llm_api_log SET request_payload = NULL, response_payload = NULL
                 WHERE (request_payload IS NOT NULL OR response_payload IS NOT NULL)
                   AND livechat_channel_id IN (SELECT id FROM im_livechat_channel
                                                WHERE ai_log_payload AND ai_assistant_id IS NULL)
            """)
            stats['logs_erased'] = cr.rowcount
        if not rows:
            self._migration_tag_bot_partners(stats)
            return stats

        lang = self._migration_lang()
        env_lang = self.with_context(lang=lang).env
        Connection = self.sudo().with_context(active_test=False, tracking_disable=True, **{NO_IN_USE_CHECK: True})
        Assistant = self.env['im_livechat.ai.assistant'].sudo().with_context(
            active_test=False, tracking_disable=True, **{NO_IN_USE_CHECK: True})
        Partner = self.env['res.partner'].sudo().with_context(active_test=False)
        default_bot = self.env.ref(DEFAULT_BOT_XMLID, raise_if_not_found=False)
        used_partners = set(Assistant.search([('partner_id', '!=', False)]).partner_id.ids)
        taken_names = set(Connection.search([]).mapped('name'))
        # candidates for reuse: connections of an earlier run (active) and of this run
        candidates = {}
        for connection in Connection.search([('legacy_migrated', '=', True), ('active', '=', True)]):
            key = (connection.backend_type, (connection.endpoint_url or '').strip() or None,
                   (connection.model or '').strip() or None, connection.document_mode,
                   connection.timeout_seconds, connection.max_retries)
            candidates.setdefault(key, []).append(connection.id)
        channels_of = {}
        created_this_run = set()

        for row in rows:
            values = normalize_legacy_row(row)
            key = tuple(values[name] for name in CONNECTION_KEY)
            connection_id = None
            if candidates.get(key):
                # equal key? compared in SQL only
                cr.execute("""
                    SELECT c.id FROM im_livechat_ai_connection c, im_livechat_channel ch
                     WHERE ch.id = %%s AND c.id IN %%s AND c.api_key_secret IS NOT DISTINCT FROM %s
                     ORDER BY c.id LIMIT 1
                """ % (NORMALIZED_KEY_SQL if has_token else 'NULL'), (row['id'], tuple(candidates[key])))
                found = cr.fetchone()
                connection_id = found[0] if found else None
            if connection_id:
                if connection_id not in created_this_run:
                    stats['connections_reused'] += 1
            else:
                name = env_lang._('%(channel)s (AI Connection)', channel=row['name'] or '?')
                if name in taken_names:
                    name = '%s #%s' % (name, row['id'])
                taken_names.add(name)
                connection = Connection.create({
                    'name': name,
                    'backend_type': values['backend_type'],
                    'model': values['model'] or False,
                    'timeout_seconds': values['timeout_seconds'],
                    'max_retries': values['max_retries'],
                    'document_mode': values['document_mode'],
                    'log_payload': False,
                    'legacy_migrated': True,
                    'customer_safe': values['backend_type'] == BACKEND_OPENAI,
                })
                connection.flush_recordset()
                # the URL as the runtime used it, and the key exactly as 18.0.1.x sent it, SQL to SQL
                cr.execute("""
                    UPDATE im_livechat_ai_connection c SET endpoint_url = %%s, api_key_secret = %s
                      FROM im_livechat_channel ch
                     WHERE ch.id = %%s AND c.id = %%s
                """ % (NORMALIZED_KEY_SQL if has_token else 'NULL'),
                    (values['endpoint_url'], row['id'], connection.id))
                connection.invalidate_recordset()
                connection_id = connection.id
                created_this_run.add(connection_id)
                candidates.setdefault(key, []).append(connection_id)
                stats['connections_created'].append(connection_id)
            channels_of.setdefault(connection_id, []).append(row['id'])

            # one assistant per channel, reusing the channel's bot partner when it is a bot
            partner = Partner.browse(row['ai_bot_partner_id']).exists() if row['ai_bot_partner_id'] else Partner
            if partner and (partner.user_ids or partner == default_bot or partner.id in used_partners):
                partner = Partner
            image = (partner.image_1920 if partner else False) or bot_avatar()
            assistant_vals = {
                'name': (row['ai_bot_name'] or '').strip() or env_lang._(
                    'AI Customer Service Assistant'),
                'description': env_lang._(
                    'Created by the 18.0.2.0.0 upgrade from the settings of the Live Chat channel "%(channel)s" '
                    '(id %(id)s).', channel=row['name'] or '?', id=row['id']),
                'connection_id': connection_id,
                'system_prompt': row['ai_system_prompt'] or False,
                'max_history': values['max_history'],
                'error_message': row['ai_error_message'] or False,
                'livechat_available': True,
                'image_1920': image,
            }
            if partner:
                assistant_vals['partner_id'] = partner.id
                used_partners.add(partner.id)
                stats['partners_reused'] += 1
            else:
                stats['partners_created'] += 1
            assistant = Assistant.create(assistant_vals)
            if not partner:
                used_partners.add(assistant.partner_id.id)
            cr.execute("UPDATE im_livechat_channel SET ai_assistant_id = %s WHERE id = %s AND ai_assistant_id IS NULL",
                       (assistant.id, row['id']))
            stats['assistants'].append(assistant.id)
            stats['channels'].append(row['id'])

        self.env['im_livechat.channel'].invalidate_model(['ai_assistant_id'])
        # connection descriptions name their channels (ids only)
        for connection_id, channel_ids in channels_of.items():
            if connection_id in created_this_run:
                Connection.browse(connection_id).description = env_lang._(
                    'Created by the 18.0.2.0.0 upgrade from the backend settings of the Live Chat channel(s) '
                    '%(ids)s.', ids=', '.join(str(i) for i in channel_ids))
        self.env.flush_all()
        # the old call logs of the migrated channels belong to their assistant and connection
        if sql.table_exists(cr, 'llm_api_log'):
            cr.execute("""
                UPDATE llm_api_log l SET assistant_id = a.id, connection_id = a.connection_id
                  FROM im_livechat_channel c JOIN im_livechat_ai_assistant a ON a.id = c.ai_assistant_id
                 WHERE l.livechat_channel_id = c.id AND l.assistant_id IS NULL AND c.id IN %s
            """, (tuple(stats['channels']),))
            stats['logs_linked'] = cr.rowcount
        self._migration_tag_bot_partners(stats)
        # AI-enabled channels that cannot answer (e.g. AI on without a URL in 18.0.1.x)
        channels = self.env['im_livechat.channel'].sudo().browse(stats['channels'])
        stats['blocked'] = channels.filtered(lambda c: c.ai_enabled and c._ai_problems()).ids
        return stats

    @api.model
    def _migration_tag_bot_partners(self, stats):
        """Every bot partner of a channel (reused or not) without a user gets the
        「AI 機器人」 tag when it is missing: messages of a former bot stay bot
        turns."""
        cr = self.env.cr
        category = self.env.ref('im_livechat_ai.partner_category_ai_bot', raise_if_not_found=False)
        if not category or not sql.column_exists(cr, TABLE, 'ai_bot_partner_id'):
            return
        cr.execute("SELECT DISTINCT ai_bot_partner_id FROM im_livechat_channel WHERE ai_bot_partner_id IS NOT NULL")
        partner_ids = [row[0] for row in cr.fetchall()]
        partner_ids += self.env['im_livechat.ai.assistant'].sudo().with_context(active_test=False).search(
            [('partner_id', '!=', False)]).partner_id.ids
        partners = self.env['res.partner'].sudo().with_context(active_test=False).browse(set(partner_ids)).exists()
        untagged = partners.filtered(lambda p: not p.user_ids and category not in p.category_id)
        if untagged:
            untagged.write({'category_id': [(4, category.id)]})
        stats['partners_tagged'] = len(untagged)
