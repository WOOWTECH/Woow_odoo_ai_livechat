# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import api, fields, models

from .im_livechat_channel import (
    BACKEND_N8N,
    BACKEND_OPENAI,
    DEFAULT_LOG_RETENTION_DAYS,
    MAX_LOG_RETENTION_DAYS,
    MIN_LOG_RETENTION_DAYS,
)

_logger = logging.getLogger(__name__)


class LLMApiLog(models.Model):
    """
    AI backend calls, for monitoring and debugging (AI API 日誌).

    Readable by Live Chat administrators only. By default a log keeps only
    timestamp, backend, model, token usage, timing, status, HTTP status and a
    short error; the request/response payloads (conversation content) are
    stored only while the channel's "Log Conversation Content" is on. Logs
    older than the channel's retention (default 14 days) are removed by a
    daily cron.
    """
    _name = 'llm.api.log'
    _description = 'AI API Log'
    _order = 'timestamp desc'
    _rec_name = 'timestamp'

    # Core Fields
    timestamp = fields.Datetime(
        string='Timestamp',
        default=fields.Datetime.now,
        required=True,
        index=True,
        help='When the API call was made',
    )

    # Related Records
    livechat_channel_id = fields.Many2one(
        comodel_name='im_livechat.channel',
        string='Livechat Channel',
        ondelete='set null',
        index=True,
        help='The livechat channel associated with this API call',
    )
    discuss_channel_id = fields.Many2one(
        comodel_name='discuss.channel',
        string='Chat Session',
        ondelete='set null',
        index=True,
        help='The chat session associated with this API call',
    )

    # Backend
    backend_type = fields.Selection(
        selection=[
            (BACKEND_OPENAI, 'OpenAI compatible (LiteLLM, Hermes…)'),
            (BACKEND_N8N, 'n8n Webhook'),
        ],
        string='Backend Type',
        help='Backend type of the channel at the time of the call',
    )
    model = fields.Char(
        string='Model',
        help='Model name sent with the call (OpenAI compatible backends; empty when '
             'not set or for n8n)',
    )
    http_status = fields.Integer(
        string='HTTP Status',
        help='HTTP status code of the backend answer (empty when no answer arrived)',
    )

    # Status
    status = fields.Selection(
        selection=[
            ('success', 'Success'),
            ('error', 'Error'),
            ('retry', 'Retry'),
        ],
        string='Status',
        required=True,
        index=True,
        help='Status of the API call',
    )

    # Payload Data
    request_payload = fields.Text(
        string='Request Payload',
        help='The request body sent to the backend (JSON, without the token). Only '
             'stored while "Log Conversation Content" is on for the channel.',
    )
    response_payload = fields.Text(
        string='Response Payload',
        help='The answer received from the backend (JSON). Only stored while '
             '"Log Conversation Content" is on for the channel.',
    )

    # Token Usage
    prompt_tokens = fields.Integer(
        string='Prompt Tokens',
        default=0,
        help='Number of tokens in the prompt',
    )
    completion_tokens = fields.Integer(
        string='Completion Tokens',
        default=0,
        help='Number of tokens in the completion',
    )
    total_tokens = fields.Integer(
        string='Total Tokens',
        default=0,
        help='Total number of tokens used',
    )

    # Performance
    response_time = fields.Float(
        string='Response Time (s)',
        help='Time taken to receive response in seconds',
    )

    # Error Info
    error_message = fields.Text(
        string='Error Message',
        help='Short error message if the API call failed (no upstream response body)',
    )
    retry_count = fields.Integer(
        string='Retry Count',
        default=0,
        help='Number of retry attempts made',
    )

    @api.model
    def _erase_payloads(self, livechat_channel_ids=None):
        """Remove stored conversation content (of all logs, or of the logs of
        the given live chat channels)."""
        domain = ['|', ('request_payload', '!=', False), ('response_payload', '!=', False)]
        if livechat_channel_ids is not None:
            domain = [('livechat_channel_id', 'in', list(livechat_channel_ids))] + domain
        logs = self.search(domain)
        if logs:
            logs.write({'request_payload': False, 'response_payload': False})
            _logger.info("Erased conversation content from %s AI API logs", len(logs))
        return len(logs)

    @api.model
    def _retention_by_channel(self):
        """{live chat channel id: retention days} for every channel (archived
        ones included), bounded to 1-3650; invalid values use 14 days."""
        Channel = self.env['im_livechat.channel'].sudo().with_context(active_test=False)
        retention = {}
        for channel in Channel.search([]):
            days = channel.ai_log_retention_days
            if not days or days < MIN_LOG_RETENTION_DAYS:
                days = DEFAULT_LOG_RETENTION_DAYS
            retention[channel.id] = min(days, MAX_LOG_RETENTION_DAYS)
        return retention

    def _cleanup_old_logs(self):
        """
        Delete API logs older than their channel's retention (default 14
        days; logs without a channel use 14 days). Finished AI reply jobs
        follow the same retention.

        Called daily by a scheduled action.

        Returns:
            int: Number of logs deleted
        """
        Log = self.sudo()
        Job = self.env['im_livechat.ai.job']
        now = fields.Datetime.now()
        by_days = {}
        for channel_id, days in self._retention_by_channel().items():
            by_days.setdefault(days, []).append(channel_id)
        count = 0
        for days, channel_ids in by_days.items():
            old_logs = Log.search([
                ('livechat_channel_id', 'in', channel_ids),
                ('timestamp', '<', now - timedelta(days=days)),
            ])
            count += len(old_logs)
            old_logs.unlink()
            Job._cleanup_old_jobs(days, channel_ids)
        orphan_logs = Log.search([
            ('livechat_channel_id', '=', False),
            ('timestamp', '<', now - timedelta(days=DEFAULT_LOG_RETENTION_DAYS)),
        ])
        count += len(orphan_logs)
        orphan_logs.unlink()
        Job._cleanup_old_jobs(DEFAULT_LOG_RETENTION_DAYS, [False])
        if count:
            _logger.info("Cleaned up %s AI API logs older than their channel retention", count)
        return count
