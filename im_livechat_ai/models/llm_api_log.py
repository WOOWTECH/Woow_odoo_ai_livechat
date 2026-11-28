# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import api, fields, models

from ..tools.backend import BACKEND_N8N, BACKEND_OPENAI

_logger = logging.getLogger(__name__)

# Call logs and finished reply jobs are kept this long (aligned with Discuss AI
# and Project AI; the monthly usage is kept).
LOG_RETENTION_DAYS = 30


class LLMApiLog(models.Model):
    """
    Call logs of the Live Chat AI (「呼叫紀錄」): one row per request the module builds (its retries stay in that
    row; each request of the file fallback is a new row).

    Readable by system administrators only. A row keeps the time, the AI
    assistant and connection, the backend, model, token usage, duration,
    status, HTTP status and the exception type name of a failure; the request
    and response content (conversation content) is stored only while the AI
    connection's "Log Request Content" is on. Rows older than 30 days are
    removed by a daily cron, together with the finished reply jobs.

    18.0.1.x wrote one row per call with the statuses success / error /
    retry; those rows stay readable until they age out.
    """
    _name = 'llm.api.log'
    _description = 'Live Chat AI Call Log'
    _order = 'timestamp desc, id desc'
    _rec_name = 'timestamp'

    timestamp = fields.Datetime(
        string='Timestamp',
        default=fields.Datetime.now,
        required=True,
        index=True,
        help='When the answer was asked',
    )

    # Related Records
    livechat_channel_id = fields.Many2one(
        comodel_name='im_livechat.channel',
        string='Live Chat Channel',
        ondelete='set null',
        index=True,
    )
    discuss_channel_id = fields.Many2one(
        comodel_name='discuss.channel',
        string='Conversation',
        ondelete='set null',
        index=True,
    )
    assistant_id = fields.Many2one(
        comodel_name='im_livechat.ai.assistant',
        string='AI Assistant',
        ondelete='set null',
        index=True,
    )
    connection_id = fields.Many2one(
        comodel_name='im_livechat.ai.connection',
        string='AI Connection',
        ondelete='set null',
        index=True,
    )
    job_id = fields.Many2one(
        comodel_name='im_livechat.ai.job',
        string='Reply Job',
        ondelete='set null',
        index='btree_not_null',
    )
    surface_type = fields.Char(string='Used In', help='livechat: a Live Chat session.')

    # Backend
    backend_type = fields.Selection(
        selection=[
            (BACKEND_OPENAI, 'LiteLLM (OpenAI compatible)'),
            (BACKEND_N8N, 'n8n webhook'),
        ],
        string='Backend',
        help='Backend type of the AI connection at the time of the call',
    )
    model = fields.Char(
        string='Model',
        help='Model name sent with the call (LiteLLM; empty for n8n)',
    )
    http_status = fields.Integer(
        string='HTTP Status',
        help='HTTP status code of the last backend answer (empty when no answer arrived)',
    )

    # Status
    status = fields.Selection(
        selection=[
            ('ok', 'OK'),
            ('http_error', 'HTTP Error'),
            ('timeout', 'Timeout'),
            ('invalid_response', 'Invalid Response'),
            ('error', 'Error'),
            # 18.0.1.x rows (one per call), kept readable until they age out
            ('success', 'Success (1.x)'),
            ('retry', 'Retry (1.x)'),
        ],
        string='Status',
        required=True,
        index=True,
    )

    # Payload Data
    request_payload = fields.Text(
        string='Request Content',
        help='The request body sent to the backend (JSON, without the key). Only stored while "Log '
             'Request Content" is on for the AI connection.',
    )
    response_payload = fields.Text(
        string='Response Content',
        help='The answer received from the backend (JSON). Only stored while "Log Request Content" is on '
             'for the AI connection.',
    )

    # Token Usage
    prompt_tokens = fields.Integer(string='Prompt Tokens', default=0)
    completion_tokens = fields.Integer(string='Completion Tokens', default=0)
    total_tokens = fields.Integer(string='Total Tokens', default=0)

    # Performance
    duration_ms = fields.Integer(string='Duration (ms)', help='Time of the whole answer, retries included')
    response_time = fields.Float(
        string='Response Time (s)',
        help='Time of the whole answer in seconds (18.0.1.x: of one call)',
    )

    # Error Info
    error_message = fields.Text(
        string='Error',
        help='The exception type name of a failed answer (never the upstream answer or the key)',
    )
    retry_count = fields.Integer(
        string='Retries',
        default=0,
        help='Retries made for this answer',
    )

    @api.model
    def _erase_payloads(self, livechat_channel_ids=None, connection_ids=None):
        """Remove stored conversation content (of all logs, of the logs of the
        given live chat channels, or of the given AI connections)."""
        domain = ['|', ('request_payload', '!=', False), ('response_payload', '!=', False)]
        if livechat_channel_ids is not None:
            domain = [('livechat_channel_id', 'in', list(livechat_channel_ids))] + domain
        if connection_ids is not None:
            domain = [('connection_id', 'in', list(connection_ids))] + domain
        logs = self.sudo().search(domain)
        if logs:
            logs.write({'request_payload': False, 'response_payload': False})
            _logger.info("Erased conversation content from %s Live Chat AI call logs", len(logs))
        return len(logs)

    def _cleanup_old_logs(self, days=LOG_RETENTION_DAYS):
        """Delete call logs and finished reply jobs older than ``days`` (30), except the jobs that remember
        files the model could not read (see im_livechat.ai.job._cleanup_old_jobs). Called daily by a scheduled
        action. Returns the number of logs deleted."""
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.sudo().search([('timestamp', '<', cutoff)])
        count = len(old)
        if count:
            old.unlink()
            _logger.info("Cleaned up %s Live Chat AI call logs older than %s days", count, days)
        self.env['im_livechat.ai.job']._cleanup_old_jobs(days)
        return count
