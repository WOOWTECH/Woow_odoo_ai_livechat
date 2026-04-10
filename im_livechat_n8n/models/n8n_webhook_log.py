# -*- coding: utf-8 -*-

import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class N8NWebhookLog(models.Model):
    """
    Log all webhook activity for n8n integration.

    This model tracks both outbound webhooks (Odoo to n8n) and inbound webhooks
    (n8n to Odoo) for monitoring, debugging, and compliance purposes.

    Features:
    - Automatic cleanup of old logs via scheduled action
    - Detailed request/response tracking
    - Performance monitoring (response time)
    - Error tracking for troubleshooting
    """
    _name = 'n8n.webhook.log'
    _description = 'N8N Webhook Log'
    _order = 'timestamp desc'
    _rec_name = 'timestamp'

    # Core Fields
    timestamp = fields.Datetime(
        string='Timestamp',
        default=fields.Datetime.now,
        required=True,
        index=True,
        help='When the webhook was triggered'
    )

    event_type = fields.Selection(
        selection=[
            ('outbound', 'Outbound (Odoo → N8N)'),
            ('inbound', 'Inbound (N8N → Odoo)'),
        ],
        string='Direction',
        required=True,
        index=True,
        help='Direction of the webhook call'
    )

    # Related Records
    livechat_channel_id = fields.Many2one(
        comodel_name='im_livechat.channel',
        string='Livechat Channel',
        ondelete='set null',
        index=True,
        help='The livechat channel associated with this webhook call'
    )

    session_id = fields.Many2one(
        comodel_name='discuss.channel',
        string='Chat Session',
        ondelete='set null',
        index=True,
        help='The chat session associated with this webhook call'
    )

    # Status and Performance
    status = fields.Selection(
        selection=[
            ('success', 'Success'),
            ('failed', 'Failed'),
            ('timeout', 'Timeout'),
        ],
        string='Status',
        required=True,
        index=True,
        help='Status of the webhook call'
    )

    response_time = fields.Float(
        string='Response Time (ms)',
        help='Time taken to receive response in milliseconds'
    )

    http_status = fields.Integer(
        string='HTTP Status Code',
        help='HTTP status code returned by the webhook endpoint'
    )

    # Payload Data
    request_payload = fields.Text(
        string='Request Payload',
        help='JSON payload sent to the webhook endpoint'
    )

    response_payload = fields.Text(
        string='Response Payload',
        help='JSON response received from the webhook endpoint'
    )

    error_message = fields.Text(
        string='Error Message',
        help='Error message if the webhook call failed'
    )

    def _cleanup_old_logs(self):
        """
        Cleanup webhook logs older than 30 days.

        This method is called by a scheduled action to prevent the log table
        from growing indefinitely. Adjust the retention period by modifying
        the timedelta parameter.

        Returns:
            int: Number of records deleted
        """
        cutoff_date = fields.Datetime.now() - timedelta(days=30)
        old_logs = self.search([('timestamp', '<', cutoff_date)])
        count = len(old_logs)

        if count > 0:
            old_logs.unlink()
            _logger.info(f"Cleaned up {count} webhook logs older than 30 days")
        else:
            _logger.debug("No webhook logs to clean up")

        return count
