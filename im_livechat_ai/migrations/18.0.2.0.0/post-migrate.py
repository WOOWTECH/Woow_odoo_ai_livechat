# -*- coding: utf-8 -*-
"""18.0.2.0.0 post-migration: the Live Chat channels' backend settings become
AI connections and AI assistants (models/ai_migration.py).

1. ``im_livechat.ai.connection._migrate_legacy_channels()``: one AI assistant
   per channel that had an AI set up, one AI connection per distinct backend
   setting (the key copied and compared in SQL only), links in SQL, content
   logging off and the stored content erased, old call logs linked. One INFO
   line with counts and ids, one WARNING with the ids of AI-enabled channels
   that cannot answer.
2. Hermes is not supported any more (n8n or LiteLLM only): the connections
   whose host is named like Hermes or whose model is ``hermes-agent``, and
   the LiteLLM connections set up the way Hermes ones were (no model, or
   documents as links only: possibly Hermes), are listed (INFO, ids only).
   They are not changed.
3. Translations: Odoo 18 keeps the old translation of a field label, help or
   selection value whose English changed, and loads the module's .po without
   overwriting right after this script. So this module's shipped translations
   are loaded once over the old ones, for this module's own records only, in
   every installed language but English. A translation an administrator
   changed for one of them is replaced by the shipped one. Records this
   module shares with other modules (the core models it extends) get their
   translations back as they were before the load.
"""

import json
import logging
import traceback
from urllib.parse import urlsplit

from odoo import SUPERUSER_ID, api
from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

MODULE = 'im_livechat_ai'


def hermes_connections(cr):
    """Ids of the connections whose host name contains "hermes" or whose model is hermes-agent."""
    if not sql.table_exists(cr, 'im_livechat_ai_connection'):
        return []
    cr.execute("SELECT id, endpoint_url, model FROM im_livechat_ai_connection ORDER BY id")
    found = []
    for connection_id, url, model in cr.fetchall():
        try:
            host = (urlsplit((url or '').strip()).hostname or '').lower()
        except ValueError:
            host = ''
        if 'hermes' in host or (model or '').strip().lower() == 'hermes-agent':
            found.append(connection_id)
    return found


def possibly_hermes_connections(cr):
    """Ids of the LiteLLM connections with no model or with documents as links only."""
    if not sql.table_exists(cr, 'im_livechat_ai_connection'):
        return []
    cr.execute("""SELECT id FROM im_livechat_ai_connection
                   WHERE backend_type = 'openai' AND (COALESCE(btrim(model), '') = '' OR document_mode = 'link')
                   ORDER BY id""")
    return [row[0] for row in cr.fetchall()]


def shared_translations(env):
    """Raw translated values of this module's records that other modules own too (the core models it
    extends), as (table, column, id, value): put back after the reload."""
    cr = env.cr
    cr.execute("""SELECT DISTINCT d.model, d.res_id FROM ir_model_data d
                    JOIN ir_model_data o ON o.model = d.model AND o.res_id = d.res_id AND o.module <> d.module
                   WHERE d.module = %s ORDER BY d.model, d.res_id""", (MODULE,))
    kept = []
    for model_name, res_id in cr.fetchall():
        if model_name not in env:
            continue
        model = env[model_name]
        for name, field in model._fields.items():
            if not (field.translate and field.store and field.column_type):
                continue
            cr.execute(SQL('SELECT %s FROM %s WHERE id = %s', SQL.identifier(name), SQL.identifier(model._table),
                           res_id))
            row = cr.fetchone()
            if row:
                kept.append((model._table, name, res_id, row[0]))
    return kept


def reload_translations(cr):
    """Load im_livechat_ai's .po files over the existing translations of this module's own records, for
    the installed languages other than English. Returns the language codes."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    langs = [code for code, _name in env['res.lang'].get_installed() if code != 'en_US']
    if not langs:
        return langs
    kept = shared_translations(env)
    env.flush_all()
    env['ir.module.module'].search([('name', '=', MODULE)])._update_translations(filter_lang=langs, overwrite=True)
    for table, column, res_id, value in kept:
        cr.execute(SQL('UPDATE %s SET %s = %s WHERE id = %s', SQL.identifier(table), SQL.identifier(column),
                       None if value is None else json.dumps(value), res_id))
    env.invalidate_all()
    return langs


def long_retention_channels(cr):
    """Ids of the channels whose 18.0.1.x Log Retention (days) was above the fixed 30 days of 18.0.2.0.0."""
    if not sql.column_exists(cr, 'im_livechat_channel', 'ai_log_retention_days'):
        return []
    cr.execute("SELECT id FROM im_livechat_channel WHERE ai_log_retention_days > 30 ORDER BY id")
    return [row[0] for row in cr.fetchall()]


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    stats = env['im_livechat.ai.connection']._migrate_legacy_channels()
    _logger.info(
        "im_livechat_ai 18.0.2.0.0: %s live chat channel(s) migrated %s; AI connection(s) created %s, reused %s; "
        "AI assistant(s) created %s; bot partner(s) reused %s, created %s, tagged %s; call log(s) with stored "
        "content erased %s, linked to their assistant %s",
        len(stats['channels']), stats['channels'], stats['connections_created'], stats['connections_reused'],
        stats['assistants'], stats['partners_reused'], stats['partners_created'], stats['partners_tagged'],
        stats['logs_erased'], stats['logs_linked'])
    if stats['blocked']:
        _logger.warning("im_livechat_ai 18.0.2.0.0: live chat channel(s) %s have the AI on but cannot answer (their "
                        "AI assistant or AI connection is not complete); they create no reply job until a system "
                        "administrator completes it (Live Chat > Configuration > AI Connections)", stats['blocked'])
    found = hermes_connections(cr)
    if found:
        _logger.info("im_livechat_ai 18.0.2.0.0: Hermes is not supported any more. AI connection(s) %s point at a "
                     "host named like Hermes or use the model hermes-agent; they are kept as they are. Point them "
                     "at LiteLLM or n8n, or archive them", found)
    possible = [connection for connection in possibly_hermes_connections(cr) if connection not in found]
    if possible:
        _logger.info("im_livechat_ai 18.0.2.0.0: possibly Hermes: LiteLLM AI connection(s) %s have no model or "
                     "send documents as links only, the way Hermes ones were set up. Check them", possible)
    longer = long_retention_channels(cr)
    if longer:
        _logger.info("im_livechat_ai 18.0.2.0.0: live chat channel(s) %s kept their call logs longer than 30 days; "
                     "18.0.2.0.0 keeps call logs and finished reply jobs 30 days, and the next daily cleanup "
                     "deletes older ones", longer)
    # Translations are labels only: a failure here must not stop the upgrade of the data moved above.
    try:
        with cr.savepoint():
            langs = reload_translations(cr)
    except Exception as e:  # noqa: BLE001 - the type name and code frames, never the text (contract 3.9)
        _logger.warning("im_livechat_ai 18.0.2.0.0: the translations of this module could not be reloaded "
                        "(%s); load them from Settings > Translations > Load a Translation: %s",
                        type(e).__name__, ''.join(traceback.format_tb(e.__traceback__)).rstrip())
        return
    if langs:
        _logger.info("im_livechat_ai 18.0.2.0.0: translations of this module reloaded over the old ones (%s)",
                     ', '.join(langs))
