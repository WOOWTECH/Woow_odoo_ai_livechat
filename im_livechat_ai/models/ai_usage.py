# -*- coding: utf-8 -*-
"""Monthly Live Chat AI usage (「AI 用量」): how many replies each AI assistant
gave, per connection, surface and Live Chat channel, and the tokens the
backends reported.

A reference for invoicing by hand, never a billing gate: nothing is limited
here (limits live in the LiteLLM keys). Rows are written only by the reply
jobs, with one SQL upsert in the transaction that posts the reply (or the
error message), and are never deleted automatically (the call logs are kept
30 days only). System administrators can read them; nobody edits them through
the ORM.

Months are UTC calendar months (``month`` = their first day). n8n reports no
tokens: 0 is recorded, never an estimate. The scope is the Live Chat channel
(``scope_model`` / ``scope_res_id``, no foreign key, so deleting a channel
keeps its rows); ``livechat_channel_id`` shows it while the channel exists.

Aligned with Discuss AI and Project AI (``woow.*.usage.month``); nothing is
shared with those modules.
"""

import logging

from odoo import api, fields, models
from odoo.tools import sql

_logger = logging.getLogger(__name__)

KEY_INDEX = 'im_livechat_ai_usage_month_key_uniq'
KEY_EXPRESSIONS = ('assistant_id', 'connection_id', "COALESCE(scope_model, '')", 'COALESCE(scope_res_id, 0)',
                   "COALESCE(surface_type, '')", 'month')
SURFACE_LIVECHAT = 'livechat'
SCOPE_MODEL = 'im_livechat.channel'


class LivechatAiUsageMonth(models.Model):
    _name = 'im_livechat.ai.usage.month'
    _description = 'Live Chat AI Usage per Month'
    _order = 'month desc, assistant_id, id'

    month = fields.Date(string='Month', required=True, readonly=True, index=True,
                        help='First day of the month (UTC).')
    assistant_id = fields.Many2one('im_livechat.ai.assistant', string='AI Assistant', required=True,
                                   readonly=True, ondelete='restrict', index=True)
    connection_id = fields.Many2one('im_livechat.ai.connection', string='AI Connection', required=True,
                                    readonly=True, ondelete='restrict', index=True)
    surface_type = fields.Char(string='Used In', readonly=True,
                               help='livechat: the Live Chat sessions of a channel.')
    scope_model = fields.Char(string='Scope Model', readonly=True)
    scope_res_id = fields.Many2oneReference(string='Scope Record', model_field='scope_model', readonly=True,
                                            help='What the replies are counted for: the Live Chat channel.')
    livechat_channel_id = fields.Many2one('im_livechat.channel', string='Live Chat Channel', readonly=True,
                                          index=True, ondelete='set null',
                                          help='Empty once the channel is deleted; the scope keeps its id.')
    replies_ok = fields.Integer(string='Replies', readonly=True,
                                help='Replies of the backend posted in the conversations.')
    replies_failed = fields.Integer(string='Failed Replies', readonly=True,
                                    help='Replies that failed: the visitor got the error message.')
    prompt_tokens = fields.Integer(string='Prompt Tokens', readonly=True)
    completion_tokens = fields.Integer(string='Completion Tokens', readonly=True)
    total_tokens = fields.Integer(string='Total Tokens', readonly=True)

    def init(self):
        # one row per key; NULL scopes count as the same key (COALESCE)
        sql.create_unique_index(self.env.cr, KEY_INDEX, self._table, list(KEY_EXPRESSIONS))

    @api.depends('assistant_id', 'month')
    def _compute_display_name(self):
        for row in self:
            row.display_name = '%s %s' % (row.assistant_id.display_name or '?',
                                          row.month.strftime('%Y-%m') if row.month else '')

    @api.model
    def _add(self, assistant, connection, livechat_channel=None, ok=True, prompt_tokens=0, completion_tokens=0):
        """Count one reply (``ok``) or one failed reply in the current UTC
        month, in the current transaction (one statement, safe against
        concurrent replies: the conflicting row is locked and incremented)."""
        if not assistant or not connection:
            return
        channel_id = livechat_channel.id if livechat_channel else None
        self.env.cr.execute("""
            INSERT INTO im_livechat_ai_usage_month
                   (assistant_id, connection_id, surface_type, scope_model, scope_res_id, livechat_channel_id,
                    month, replies_ok, replies_failed, prompt_tokens, completion_tokens, total_tokens,
                    create_uid, create_date, write_uid, write_date)
            VALUES (%(assistant)s, %(connection)s, %(surface)s, %(scope_model)s, %(scope_res_id)s, %(channel)s,
                    date_trunc('month', statement_timestamp() AT TIME ZONE 'UTC')::date,
                    %(ok)s, %(failed)s, %(prompt)s, %(completion)s, %(prompt)s + %(completion)s,
                    %(uid)s, statement_timestamp() AT TIME ZONE 'UTC',
                    %(uid)s, statement_timestamp() AT TIME ZONE 'UTC')
            ON CONFLICT (assistant_id, connection_id, (COALESCE(scope_model, '')), (COALESCE(scope_res_id, 0)),
                         (COALESCE(surface_type, '')), month)
            DO UPDATE SET replies_ok = im_livechat_ai_usage_month.replies_ok + EXCLUDED.replies_ok,
                          replies_failed = im_livechat_ai_usage_month.replies_failed + EXCLUDED.replies_failed,
                          prompt_tokens = im_livechat_ai_usage_month.prompt_tokens + EXCLUDED.prompt_tokens,
                          completion_tokens = im_livechat_ai_usage_month.completion_tokens
                                              + EXCLUDED.completion_tokens,
                          total_tokens = im_livechat_ai_usage_month.total_tokens + EXCLUDED.total_tokens,
                          livechat_channel_id = COALESCE(EXCLUDED.livechat_channel_id,
                                                         im_livechat_ai_usage_month.livechat_channel_id),
                          write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date
        """, {
            'assistant': assistant.id,
            'connection': connection.id,
            'surface': SURFACE_LIVECHAT,
            'scope_model': SCOPE_MODEL if channel_id else None,
            'scope_res_id': channel_id,
            'channel': channel_id,
            'ok': 1 if ok else 0,
            'failed': 0 if ok else 1,
            'prompt': max(0, int(prompt_tokens or 0)),
            'completion': max(0, int(completion_tokens or 0)),
            'uid': self.env.uid,
        })
        self.invalidate_model()
