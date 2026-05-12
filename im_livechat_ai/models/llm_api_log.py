# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)


class LLMApiLog(models.Model):
    """
    Log all LLM API call activity for monitoring and debugging.

    Tracks each API call with request/response payloads, token usage,
    response times, and error information. Includes automatic cleanup
    of old logs via scheduled action.
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
        help='The messages array sent to the LLM API (JSON)',
    )
    response_payload = fields.Text(
        string='Response Payload',
        help='The response received from the LLM API (JSON)',
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
        help='Error message if the API call failed',
    )
    retry_count = fields.Integer(
        string='Retry Count',
        default=0,
        help='Number of retry attempts made',
    )

    def _cleanup_old_logs(self):
        """
        Cleanup API logs older than 30 days.

        Called by a scheduled action to prevent the log table
        from growing indefinitely.

        Returns:
            int: Number of records deleted
        """
        cutoff_date = fields.Datetime.now() - timedelta(days=30)
        old_logs = self.search([('timestamp', '<', cutoff_date)])
        count = len(old_logs)

        if count > 0:
            old_logs.unlink()
            _logger.info("Cleaned up %s LLM API logs older than 30 days", count)
        else:
            _logger.debug("No LLM API logs to clean up")

        return count
