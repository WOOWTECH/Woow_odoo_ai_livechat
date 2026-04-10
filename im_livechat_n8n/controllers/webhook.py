# -*- coding: utf-8 -*-

from odoo import http
from odoo.http import request
import json
import logging
import re

_logger = logging.getLogger(__name__)

# Security constants
MAX_MESSAGE_SIZE = 10240  # 10KB max message size
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


class N8NWebhookController(http.Controller):
    """
    HTTP controller for receiving webhooks from n8n.

    This controller handles inbound POST requests from n8n workflows,
    validates API keys, and posts automated messages to livechat sessions.
    """

    @http.route('/im_livechat_n8n/webhook', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def receive_webhook(self, **kwargs):
        """
        Main webhook endpoint for n8n callbacks.

        Expected payload:
        {
            "action": "send_message",
            "session_uuid": "abc-123-def",
            "message": {
                "body": "Your message here",
                "author_name": "Support Bot"
            }
        }

        Response codes:
        - 200: Message created successfully
        - 400: Invalid JSON payload
        - 401: Missing or invalid API key
        - 404: Session not found
        - 500: Server error
        """
        # Get API key from request header
        api_key = request.httprequest.headers.get('X-API-Key')
        if not api_key:
            _logger.warning("Webhook request missing API key")

            # Log missing API key attempt
            try:
                data = request.get_json_data()
            except Exception:
                data = None

            try:
                request.env['n8n.webhook.log'].sudo().create({
                    'event_type': 'inbound',
                    'status': 'failed',
                    'request_payload': json.dumps(data) if data else None,
                    'http_status': 401,
                    'error_message': 'Missing API key',
                })
            except Exception:
                pass

            return self._json_response({'error': 'Missing API key'}, 401)

        # Parse JSON body
        try:
            data = request.get_json_data()
        except Exception as e:
            _logger.error(f"Failed to parse webhook JSON: {e}")
            return self._json_response({'error': 'Invalid JSON'}, 400)

        # Validate required fields
        if not data.get('session_uuid'):
            return self._json_response({'error': 'Missing session_uuid'}, 400)

        if not data.get('message') or not data.get('message', {}).get('body'):
            return self._json_response({'error': 'Missing message body'}, 400)

        # Validate session UUID format
        session_uuid = data.get('session_uuid')
        if not self._validate_session_uuid(session_uuid):
            return self._json_response({'error': 'Invalid session_uuid format'}, 400)

        # Validate message size
        message_body = data.get('message', {}).get('body', '')
        if not self._validate_message_size(message_body):
            return self._json_response({'error': 'Message body too large (max 10KB)'}, 400)

        # Validate API key and find channel
        channel = self._validate_api_key(api_key)
        if not channel:
            _logger.warning(f"Invalid API key attempted: {api_key[:8]}...")

            # Log invalid API key attempt
            try:
                request.env['n8n.webhook.log'].sudo().create({
                    'event_type': 'inbound',
                    'status': 'failed',
                    'request_payload': json.dumps(data) if data else None,
                    'http_status': 401,
                    'error_message': 'Invalid API key',
                })
            except Exception:
                pass

            return self._json_response({'error': 'Invalid API key'}, 401)

        # Find session by UUID
        session = self._find_session(data.get('session_uuid'))
        if not session:
            _logger.warning(f"Session not found: {data.get('session_uuid')}")
            return self._json_response({'error': 'Session not found'}, 404)

        # Create message in session
        try:
            message_data = data.get('message', {})
            self._create_bot_message(session, message_data)

            _logger.info(f"Message posted to session {session.uuid} via n8n webhook")

            # Log successful inbound webhook
            try:
                request.env['n8n.webhook.log'].sudo().create({
                    'event_type': 'inbound',
                    'livechat_channel_id': channel.id,
                    'session_id': session.id,
                    'status': 'success',
                    'request_payload': json.dumps(data),
                    'http_status': 200,
                })
            except Exception as log_error:
                _logger.warning(f"Failed to log inbound webhook: {log_error}")

            return self._json_response({
                'status': 'ok',
                'session_uuid': session.uuid,
                'message': 'Message posted successfully'
            }, 200)

        except Exception as e:
            _logger.error(f"Failed to create message: {e}", exc_info=True)

            # Log failed inbound webhook
            try:
                request.env['n8n.webhook.log'].sudo().create({
                    'event_type': 'inbound',
                    'livechat_channel_id': channel.id if channel else None,
                    'session_id': session.id if session else None,
                    'status': 'failed',
                    'request_payload': json.dumps(data) if data else None,
                    'http_status': 500,
                    'error_message': str(e),
                })
            except Exception as log_error:
                _logger.warning(f"Failed to log error webhook: {log_error}")

            return self._json_response({'error': str(e)}, 500)

    def _validate_session_uuid(self, session_uuid):
        """Validate session UUID format."""
        if not session_uuid:
            return False
        return bool(UUID_PATTERN.match(str(session_uuid)))

    def _validate_message_size(self, message_body):
        """Check message body doesn't exceed max size."""
        if not message_body:
            return True
        return len(message_body.encode('utf-8')) <= MAX_MESSAGE_SIZE

    def _json_response(self, data, status):
        """
        Helper to create JSON HTTP response.

        Args:
            data (dict): Response data to be JSON encoded
            status (int): HTTP status code

        Returns:
            werkzeug.Response: HTTP response with JSON content
        """
        return request.make_response(
            json.dumps(data),
            headers=[('Content-Type', 'application/json')],
            status=status
        )

    def _validate_api_key(self, api_key):
        """
        Validate API key and return matching channel.

        Args:
            api_key (str): API key from request header

        Returns:
            im_livechat.channel: Channel record if valid, False otherwise
        """
        channel = request.env['im_livechat.channel'].sudo().search(
            [('n8n_api_key', '=', api_key)], limit=1
        )
        return channel if channel else False

    def _find_session(self, session_uuid):
        """
        Find discuss.channel (livechat session) by UUID.

        Args:
            session_uuid (str): Unique identifier for the session

        Returns:
            discuss.channel: Session record if found, False otherwise
        """
        if not session_uuid:
            return False

        session = request.env['discuss.channel'].sudo().search(
            [('uuid', '=', session_uuid)], limit=1
        )
        return session if session else False

    def _create_bot_message(self, session, message_data):
        """
        Create a message in the livechat session.

        Args:
            session (discuss.channel): The livechat session
            message_data (dict): Message details with 'body' and optional 'author_name'
        """
        body = message_data.get('body', '')
        author_name = message_data.get('author_name', 'Bot')

        # Post message to the session
        # Using message_post which is the standard Odoo method for posting messages
        session.message_post(
            body=body,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=False,  # No specific author (will show as OdooBot)
        )

        _logger.debug(f"Posted message to session {session.uuid}: {body[:50]}...")
