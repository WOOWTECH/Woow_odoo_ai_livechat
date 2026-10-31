# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import api, fields, models

from .im_livechat_channel import DEFAULT_LOG_RETENTION_DAYS, ICP_LOG_RETENTION_DAYS

_logger = logging.getLogger(__name__)


class LLMApiLog(models.Model):
    """
    Log LLM API call activity for monitoring and debugging.

    Readable by Live Chat administrators only. By default a log keeps only
    timestamp, model, token usage, timing, status and a short error; the
    request/response payloads (conversation content) are stored only while
    the system setting "Log Conversation Content" is on. Logs older than the
    configured retention (default 14 days) are removed by a daily cron.
    """
    _name = 'llm.api.log'
    _description = 'LLM API Log'
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

    # LLM Info
    model = fields.Char(
        string='Model',
        help='The LLM model name used for this call',
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
        help='The messages array sent to the LLM API (JSON). Only stored while '
             '"Log Conversation Content" is enabled in Settings > Live Chat AI.',
    )
    response_payload = fields.Text(
        string='Response Payload',
        help='The response received from the LLM API (JSON). Only stored while '
             '"Log Conversation Content" is enabled in Settings > Live Chat AI.',
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
    tool_names = fields.Char(
        string='Tools Requested',
        help='Product lookup tools the model asked for in this response '
             '(names only; the arguments are conversation content)',
    )

    @api.model
    def _get_retention_days(self):
        value = self.env['ir.config_parameter'].sudo().get_param(ICP_LOG_RETENTION_DAYS)
        try:
            days = int(value) if value else DEFAULT_LOG_RETENTION_DAYS
        except (TypeError, ValueError):
            _logger.warning("Invalid %s value %r, using %s days",
                            ICP_LOG_RETENTION_DAYS, value, DEFAULT_LOG_RETENTION_DAYS)
            days = DEFAULT_LOG_RETENTION_DAYS
        return max(1, days)

    @api.model
    def _erase_payloads(self):
        """Remove stored conversation content from all logs."""
        logs = self.search(['|', ('request_payload', '!=', False), ('response_payload', '!=', False)])
        if logs:
            logs.write({'request_payload': False, 'response_payload': False})
            _logger.info("Erased conversation content from %s LLM API logs", len(logs))
        return len(logs)

    def _cleanup_old_logs(self):
        """
        Delete API logs older than the configured retention (default 14 days).

        Called by a scheduled action to prevent the log table
        from growing indefinitely.

        Returns:
            int: Number of records deleted
        """
        days = self._get_retention_days()
        cutoff_date = fields.Datetime.now() - timedelta(days=days)
        old_logs = self.search([('timestamp', '<', cutoff_date)])
        count = len(old_logs)

        if count > 0:
            old_logs.unlink()
            _logger.info("Cleaned up %s LLM API logs older than %s days", count, days)
        else:
            _logger.debug("No LLM API logs to clean up")

        # Finished AI reply jobs follow the same retention.
        self.env['im_livechat.ai.job']._cleanup_old_jobs(days)

        return count
