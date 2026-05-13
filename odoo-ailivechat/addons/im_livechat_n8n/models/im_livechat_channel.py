# -*- coding: utf-8 -*-

import logging
import secrets
import threading
import time
import json
from datetime import datetime

import requests

from odoo import api, fields, models, _, SUPERUSER_ID
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ImLivechatChannel(models.Model):
    """
    Extend im_livechat.channel model to add n8n webhook integration.

    This extension adds:
    - n8n webhook configuration fields per channel
    - API key generation and rotation
    - Threaded webhook dispatching to n8n
    - Test webhook functionality
    """
    _inherit = 'im_livechat.channel'

    # N8N Integration Fields
    n8n_enabled = fields.Boolean(
        string='Enable N8N Integration',
        default=False,
        help='Enable webhook notifications to n8n workflow automation platform'
    )
    n8n_webhook_url = fields.Char(
        string='N8N Webhook URL',
        help='Full URL of the n8n webhook endpoint (e.g., https://n8n.example.com/webhook/abc123)'
    )
    n8n_api_key = fields.Char(
        string='API Key',
        readonly=True,
        copy=False,
        help='Auto-generated API key for authenticating incoming webhook callbacks from n8n'
    )
    n8n_max_retries = fields.Integer(
        string='Max Retries',
        default=3,
        help='Maximum number of retry attempts for failed webhook calls (1-10)',
    )
    n8n_timeout = fields.Integer(
        string='Timeout (seconds)',
        default=10,
        help='Timeout in seconds for each webhook request (1-60)',
    )

    @api.constrains('n8n_webhook_url')
    def _check_webhook_url(self):
        """Validate webhook URL format and warn about non-HTTPS URLs."""
        for record in self:
            if record.n8n_webhook_url:
                url = record.n8n_webhook_url.strip()
                # Basic URL validation
                if not url.startswith(('http://', 'https://')):
                    raise UserError(_('Webhook URL must start with http:// or https://'))
                # Warn (don't block) for non-HTTPS
                if url.startswith('http://') and 'localhost' not in url and '127.0.0.1' not in url:
                    _logger.warning("Webhook URL is not HTTPS for channel %s: %s", record.name, url)

    @api.model_create_multi
    def create(self, vals_list):
        """
        Override create to auto-generate API key for new channels.

        Uses secrets.token_urlsafe() for cryptographically strong random keys.
        """
        for vals in vals_list:
            if 'n8n_api_key' not in vals:
                vals['n8n_api_key'] = secrets.token_urlsafe(32)

        return super(ImLivechatChannel, self).create(vals_list)

    def action_regenerate_api_key(self):
        """
        Regenerate API key for the channel.

        This is useful for key rotation or if a key has been compromised.
        Can be called from a button in the UI.
        """
        self.ensure_one()

        new_key = secrets.token_urlsafe(32)
        self.write({'n8n_api_key': new_key})

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('API Key Regenerated'),
                'message': _('New API key has been generated successfully.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def _trigger_n8n_webhook(self, event_type, session, message=None):
        """
        Trigger n8n webhook asynchronously in a separate thread.

        This method is non-blocking to avoid slowing down chat operations.
        Implements retry logic with exponential backoff (3 retries: 1s, 2s, 4s delays).

        Args:
            event_type (str): Type of event (e.g., 'message_received', 'session_started', 'session_ended')
            session (mail.channel): The livechat session record
            message (mail.message, optional): The message record if applicable
        """
        self.ensure_one()

        # Skip if integration is not enabled or webhook URL is not configured
        if not self.n8n_enabled or not self.n8n_webhook_url:
            _logger.debug("N8N webhook skipped for channel %s: integration disabled or no URL configured", self.name)
            return

        # Build payload in main thread to ensure data access
        try:
            payload = self._build_webhook_payload(event_type, session, message)
        except Exception as e:
            _logger.error("Failed to build webhook payload for channel %s: %s", self.name, e, exc_info=True)
            return

        # Store IDs and config for use in thread context
        channel_id = self.id
        session_id = session.id if session else None
        webhook_url = self.n8n_webhook_url
        max_retries = max(1, min(self.n8n_max_retries or 3, 10))
        timeout = max(1, min(self.n8n_timeout or 10, 60))

        # Send webhook in separate thread to avoid blocking
        def send_webhook():

            for attempt in range(max_retries):
                start_time = time.time()
                try:
                    response = requests.post(
                        webhook_url,
                        json=payload,
                        timeout=timeout,
                        headers={'Content-Type': 'application/json'}
                    )
                    response_time = (time.time() - start_time) * 1000

                    # Log success
                    _logger.info("N8N webhook sent successfully for channel ID %s, event: %s", channel_id, event_type)
                    self._create_webhook_log(
                        channel_id,
                        'outbound',
                        'success',
                        response_time,
                        payload,
                        response.text,
                        response.status_code,
                        session_id=session_id
                    )
                    return

                except requests.Timeout:
                    response_time = timeout * 1000
                    _logger.warning("N8N webhook timeout for channel ID %s, event: %s (attempt %s/%s)", channel_id, event_type, attempt + 1, max_retries)

                    if attempt == max_retries - 1:
                        # Final timeout - log failure
                        self._create_webhook_log(
                            channel_id,
                            'outbound',
                            'timeout',
                            response_time,
                            payload,
                            None,
                            None,
                            error_message=f'Request timed out after {max_retries} retries',
                            session_id=session_id
                        )

                except requests.RequestException as e:
                    response_time = (time.time() - start_time) * 1000 if start_time else None
                    _logger.warning("N8N webhook failed for channel ID %s, event: %s: %s (attempt %s/%s)", channel_id, event_type, e, attempt + 1, max_retries)

                    if attempt == max_retries - 1:
                        # Final failure - log error
                        self._create_webhook_log(
                            channel_id,
                            'outbound',
                            'failed',
                            response_time,
                            payload,
                            None,
                            None,
                            error_message=str(e),
                            session_id=session_id
                        )

                except Exception as e:
                    # Unexpected error - log and stop retrying
                    _logger.error("Unexpected error in N8N webhook for channel ID %s: %s", channel_id, e, exc_info=True)
                    self._create_webhook_log(
                        channel_id,
                        'outbound',
                        'failed',
                        None,
                        payload,
                        None,
                        None,
                        error_message=f'Unexpected error: {str(e)}',
                        session_id=session_id
                    )
                    return

                # Exponential backoff: 1s, 2s, 4s
                if attempt < max_retries - 1:
                    backoff_time = 2 ** attempt
                    _logger.debug("Retrying webhook in %ss...", backoff_time)
                    time.sleep(backoff_time)

        # Start thread
        thread = threading.Thread(target=send_webhook, daemon=True)
        thread.start()

    def _create_webhook_log(self, channel_id, event_type, status, response_time, request_payload, response_payload, http_status, error_message=None, session_id=None):
        """
        Create webhook log entry using new cursor to avoid transaction issues in thread.

        This method creates a new database cursor to ensure log entries are persisted
        independently of the main transaction, preventing loss of logs if the main
        transaction is rolled back.

        Args:
            channel_id (int): ID of the livechat channel
            event_type (str): 'outbound' or 'inbound'
            status (str): 'success', 'failed', or 'timeout'
            response_time (float): Response time in milliseconds
            request_payload (dict): Request payload (will be JSON serialized)
            response_payload (str): Response payload text
            http_status (int): HTTP status code
            error_message (str, optional): Error message if applicable
            session_id (int, optional): ID of the chat session
        """
        try:
            with self.pool.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                # Validate FK references still exist before inserting
                # (the original transaction may have been rolled back)
                if channel_id and not env['im_livechat.channel'].browse(channel_id).exists():
                    _logger.warning("Skipping webhook log: channel %s no longer exists", channel_id)
                    return
                vals = {
                    'event_type': event_type,
                    'livechat_channel_id': channel_id,
                    'status': status,
                    'response_time': response_time,
                    'request_payload': json.dumps(request_payload) if request_payload else None,
                    'response_payload': response_payload,
                    'http_status': http_status,
                    'error_message': error_message,
                }
                if session_id:
                    if env['discuss.channel'].browse(session_id).exists():
                        vals['session_id'] = session_id
                    else:
                        _logger.warning("Webhook log: session %s no longer exists, omitting", session_id)
                env['n8n.webhook.log'].create(vals)
                cr.commit()
        except Exception as log_error:
            # Never let logging failures affect webhook operation
            _logger.error("Failed to create webhook log: %s", log_error, exc_info=True)

    def _build_webhook_payload(self, event_type, session, message=None):
        """
        Build the webhook payload structure.

        Args:
            event_type (str): Type of event
            session (mail.channel): The livechat session record
            message (mail.message, optional): The message record

        Returns:
            dict: Webhook payload ready to be sent as JSON
        """
        self.ensure_one()

        # Get base URL for callback
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')

        # Build session data
        session_data = {
            'id': session.id,
            'uuid': session.uuid if hasattr(session, 'uuid') else None,
            'name': session.name,
            'started_at': session.create_date.isoformat() if session.create_date else None,
        }

        # Add visitor (guest) information if available
        # In Odoo 18, livechat visitors are mail.guest records on channel members
        try:
            guest_member = session.channel_member_ids.filtered(lambda m: m.guest_id)
            if guest_member:
                guest = guest_member[0].guest_id
                session_data.update({
                    'visitor_name': guest.name,
                    'visitor_country': guest.country_id.code if guest.country_id else None,
                    'visitor_lang': guest.lang if guest.lang else None,
                })
        except Exception:
            pass  # Non-critical: skip visitor info if unavailable

        # Build message data if message provided
        message_data = None
        if message:
            # Determine author type
            if message.author_guest_id:
                author_type = 'visitor'
            elif not message.author_id:
                author_type = 'visitor'
            elif not message.author_id.user_ids:
                author_type = 'visitor'
            else:
                author_type = 'operator'

            # Resolve author name: prefer guest name, then partner name
            if message.author_guest_id:
                author_name = message.author_guest_id.name
            elif message.author_id:
                author_name = message.author_id.name
            else:
                author_name = None

            message_data = {
                'id': message.id,
                'body': message.body,
                'author_id': message.author_id.id if message.author_id else None,
                'author_name': author_name,
                'author_type': author_type,
                'created_at': message.create_date.isoformat() if message.create_date else None,
            }

        # Build channel data
        channel_data = {
            'id': self.id,
            'name': self.name,
        }

        # Build metadata
        metadata = {
            'odoo_base_url': base_url,
            'callback_url': f"{base_url}/im_livechat_n8n/webhook",
            'api_key_header': 'X-API-Key',
        }

        # Construct final payload
        payload = {
            'event_type': event_type,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'session': session_data,
            'message': message_data,
            'channel': channel_data,
            'metadata': metadata,
        }

        return payload

    def action_test_webhook(self):
        """
        Test the webhook connection by sending a test event.

        Returns a user notification indicating success or failure.
        Can be called from a button in the UI.
        """
        self.ensure_one()

        if not self.n8n_enabled:
            raise UserError(_('N8N integration is not enabled for this channel.'))

        if not self.n8n_webhook_url:
            raise UserError(_('N8N webhook URL is not configured for this channel.'))

        # Create a test payload
        test_payload = {
            'event_type': 'test',
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'channel': {
                'id': self.id,
                'name': self.name,
            },
            'message': _('This is a test webhook from Odoo Live Chat N8N integration.'),
        }

        # Try to send synchronously for immediate feedback
        try:
            response = requests.post(
                self.n8n_webhook_url,
                json=test_payload,
                timeout=10,
                headers={'Content-Type': 'application/json'}
            )

            if response.status_code >= 200 and response.status_code < 300:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Webhook Test Successful'),
                        'message': _('Test webhook sent successfully. Status code: %s') % response.status_code,
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Webhook Test Failed'),
                        'message': _('Webhook returned status code: %s') % response.status_code,
                        'type': 'warning',
                        'sticky': True,
                    }
                }

        except requests.exceptions.Timeout:
            raise UserError(_('Webhook request timed out. Please check the URL and try again.'))
        except requests.exceptions.ConnectionError:
            raise UserError(_('Could not connect to webhook URL. Please check the URL and network connection.'))
        except Exception as e:
            raise UserError(_('Webhook test failed: %s') % str(e))

    def action_view_webhook_logs(self):
        """
        Open the webhook logs view filtered for this channel.

        This action allows administrators to view all webhook activity
        for a specific livechat channel, helping with debugging and monitoring.

        Returns:
            dict: Action definition to open the webhook logs view
        """
        self.ensure_one()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Webhook Logs - %s') % self.name,
            'res_model': 'n8n.webhook.log',
            'view_mode': 'list,form',
            'domain': [('livechat_channel_id', '=', self.id)],
            'context': {
                'default_livechat_channel_id': self.id,
                'search_default_filter_last_7_days': 1,
            },
        }
