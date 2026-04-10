# -*- coding: utf-8 -*-

from unittest.mock import patch, MagicMock
from datetime import timedelta

from odoo.tests import TransactionCase, tagged
from odoo import fields
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestImLivechatN8N(TransactionCase):
    """Test cases for im_livechat_n8n module."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Create a test livechat channel
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Test Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'https://example.com/webhook/test',
        })

        # Create a test chat session
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Test Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.channel.id,
            'uuid': 'test-uuid-123-456',
        })

        # Create a test partner (visitor)
        cls.visitor = cls.env['res.partner'].create({
            'name': 'Test Visitor',
        })

    def test_api_key_generation(self):
        """Test that API key is auto-generated on channel creation."""
        # Channel created in setUpClass should have API key
        self.assertTrue(self.channel.n8n_api_key)
        self.assertEqual(len(self.channel.n8n_api_key), 43)  # token_urlsafe(32) produces 43 chars

    def test_api_key_auto_generation_on_create(self):
        """Test API key is generated automatically for new channels."""
        new_channel = self.env['im_livechat.channel'].create({
            'name': 'New Test Channel',
        })
        self.assertTrue(new_channel.n8n_api_key)
        self.assertGreater(len(new_channel.n8n_api_key), 0)

    def test_api_key_regeneration(self):
        """Test API key regeneration."""
        old_key = self.channel.n8n_api_key
        result = self.channel.action_regenerate_api_key()

        # Check key was changed
        self.assertNotEqual(self.channel.n8n_api_key, old_key)
        self.assertEqual(len(self.channel.n8n_api_key), 43)

        # Check notification returned
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'display_notification')

    def test_webhook_url_validation_valid_urls(self):
        """Test webhook URL validation accepts valid URLs."""
        valid_urls = [
            'https://example.com/webhook',
            'http://localhost:5678/webhook',
            'http://127.0.0.1:5678/test',
        ]

        for url in valid_urls:
            self.channel.write({'n8n_webhook_url': url})
            self.assertEqual(self.channel.n8n_webhook_url, url)

    def test_webhook_url_validation_invalid_urls(self):
        """Test webhook URL validation rejects invalid URLs."""
        invalid_urls = [
            'not-a-url',
            'ftp://invalid-protocol.com',
            'just some text',
            'www.missing-protocol.com',
        ]

        for url in invalid_urls:
            with self.assertRaises(UserError):
                self.channel.write({'n8n_webhook_url': url})

    def test_webhook_payload_structure(self):
        """Test webhook payload contains required fields."""
        # Create a test message
        message = self.env['mail.message'].create({
            'body': 'Test message',
            'author_id': self.visitor.id,
            'model': 'discuss.channel',
            'res_id': self.session.id,
        })

        payload = self.channel._build_webhook_payload('message_received', self.session, message)

        # Check top-level fields
        self.assertIn('event_type', payload)
        self.assertIn('timestamp', payload)
        self.assertIn('session', payload)
        self.assertIn('message', payload)
        self.assertIn('channel', payload)
        self.assertIn('metadata', payload)

        # Check event type
        self.assertEqual(payload['event_type'], 'message_received')

        # Check session data
        self.assertEqual(payload['session']['id'], self.session.id)
        self.assertEqual(payload['session']['uuid'], 'test-uuid-123-456')

        # Check message data
        self.assertEqual(payload['message']['id'], message.id)
        self.assertEqual(payload['message']['body'], 'Test message')

        # Check channel data
        self.assertEqual(payload['channel']['id'], self.channel.id)
        self.assertEqual(payload['channel']['name'], 'Test Channel')

        # Check metadata
        self.assertIn('odoo_base_url', payload['metadata'])
        self.assertIn('callback_url', payload['metadata'])

    def test_webhook_payload_without_message(self):
        """Test webhook payload can be built without a message."""
        payload = self.channel._build_webhook_payload('session_started', self.session)

        self.assertIn('event_type', payload)
        self.assertIn('session', payload)
        self.assertIsNone(payload['message'])

    def test_webhook_log_creation(self):
        """Test webhook log model can be created."""
        log = self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'livechat_channel_id': self.channel.id,
            'session_id': self.session.id,
            'status': 'success',
            'response_time': 150.5,
            'http_status': 200,
            'request_payload': '{"test": "data"}',
            'response_payload': '{"status": "ok"}',
        })

        self.assertEqual(log.status, 'success')
        self.assertEqual(log.livechat_channel_id, self.channel)
        self.assertEqual(log.session_id, self.session)
        self.assertEqual(log.response_time, 150.5)
        self.assertEqual(log.http_status, 200)

    def test_webhook_log_cleanup(self):
        """Test old logs are cleaned up."""
        # Create old log (31 days ago)
        old_log = self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=31),
        })

        # Create recent log (15 days ago)
        recent_log = self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=15),
        })

        # Run cleanup
        deleted_count = self.env['n8n.webhook.log']._cleanup_old_logs()

        # Verify old log deleted, recent log kept
        self.assertFalse(old_log.exists())
        self.assertTrue(recent_log.exists())
        self.assertEqual(deleted_count, 1)

    def test_action_view_webhook_logs(self):
        """Test the action to view webhook logs."""
        action = self.channel.action_view_webhook_logs()

        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'n8n.webhook.log')
        self.assertIn(('livechat_channel_id', '=', self.channel.id), action['domain'])

    @patch('requests.post')
    def test_trigger_webhook_integration_disabled(self, mock_post):
        """Test webhook not triggered when integration is disabled."""
        self.channel.n8n_enabled = False
        self.channel._trigger_n8n_webhook('test', self.session)

        # Webhook should not be called
        mock_post.assert_not_called()

    @patch('requests.post')
    def test_trigger_webhook_no_url(self, mock_post):
        """Test webhook not triggered when URL is not configured."""
        self.channel.n8n_webhook_url = False
        self.channel._trigger_n8n_webhook('test', self.session)

        # Webhook should not be called
        mock_post.assert_not_called()

    def test_action_test_webhook_not_enabled(self):
        """Test test webhook action raises error when integration not enabled."""
        self.channel.n8n_enabled = False

        with self.assertRaises(UserError):
            self.channel.action_test_webhook()

    def test_action_test_webhook_no_url(self):
        """Test test webhook action raises error when URL not configured."""
        self.channel.n8n_webhook_url = False

        with self.assertRaises(UserError):
            self.channel.action_test_webhook()

    @patch('requests.post')
    def test_action_test_webhook_success(self, mock_post):
        """Test successful webhook test."""
        # Mock successful response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = self.channel.action_test_webhook()

        # Check webhook was called
        mock_post.assert_called_once()

        # Check success notification
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')

    @patch('requests.post')
    def test_action_test_webhook_failure(self, mock_post):
        """Test failed webhook test."""
        # Mock failed response
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_post.return_value = mock_response

        result = self.channel.action_test_webhook()

        # Check warning notification
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'warning')


@tagged('post_install', '-at_install')
class TestDiscussChannel(TransactionCase):
    """Test cases for discuss.channel extensions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Create livechat channel with n8n enabled
        cls.livechat_channel = cls.env['im_livechat.channel'].create({
            'name': 'Test Livechat Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'https://example.com/webhook',
        })

        # Create livechat session
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Test Livechat Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.livechat_channel.id,
            'uuid': 'test-session-uuid',
        })

        # Create visitor partner (no user_ids)
        cls.visitor = cls.env['res.partner'].create({
            'name': 'Test Visitor',
        })

        # Create operator user and partner
        cls.operator_user = cls.env['res.users'].create({
            'name': 'Test Operator',
            'login': 'test_operator',
        })
        cls.operator = cls.operator_user.partner_id

    def test_is_visitor_message_anonymous(self):
        """Test anonymous visitor message detection."""
        message = self.env['mail.message'].create({
            'body': 'Anonymous message',
            'author_id': False,  # No author = anonymous visitor
        })

        self.assertTrue(self.session._is_visitor_message(message))

    def test_is_visitor_message_with_visitor_partner(self):
        """Test visitor partner message detection."""
        message = self.env['mail.message'].create({
            'body': 'Visitor message',
            'author_id': self.visitor.id,  # Partner without user_ids
        })

        self.assertTrue(self.session._is_visitor_message(message))

    def test_is_visitor_message_operator(self):
        """Test operator message is not considered visitor message."""
        message = self.env['mail.message'].create({
            'body': 'Operator message',
            'author_id': self.operator.id,  # Partner with user_ids
        })

        self.assertFalse(self.session._is_visitor_message(message))

    def test_should_trigger_webhook_all_conditions_met(self):
        """Test webhook should trigger when all conditions are met."""
        message = self.env['mail.message'].create({
            'body': 'Test message',
            'author_id': self.visitor.id,
        })

        self.assertTrue(self.session._should_trigger_n8n_webhook(message))

    def test_should_trigger_webhook_not_livechat(self):
        """Test webhook should not trigger for non-livechat channels."""
        regular_channel = self.env['discuss.channel'].create({
            'name': 'Regular Channel',
            'channel_type': 'channel',
        })

        message = self.env['mail.message'].create({
            'body': 'Test message',
            'author_id': self.visitor.id,
        })

        self.assertFalse(regular_channel._should_trigger_n8n_webhook(message))

    def test_should_trigger_webhook_no_livechat_channel_id(self):
        """Test webhook should not trigger when livechat_channel_id is missing."""
        session_no_channel = self.env['discuss.channel'].create({
            'name': 'Session without channel',
            'channel_type': 'livechat',
            'livechat_channel_id': False,
        })

        message = self.env['mail.message'].create({
            'body': 'Test message',
            'author_id': self.visitor.id,
        })

        self.assertFalse(session_no_channel._should_trigger_n8n_webhook(message))

    def test_should_trigger_webhook_n8n_disabled(self):
        """Test webhook should not trigger when n8n integration is disabled."""
        self.livechat_channel.n8n_enabled = False

        message = self.env['mail.message'].create({
            'body': 'Test message',
            'author_id': self.visitor.id,
        })

        self.assertFalse(self.session._should_trigger_n8n_webhook(message))

    def test_should_trigger_webhook_operator_message(self):
        """Test webhook should not trigger for operator messages."""
        message = self.env['mail.message'].create({
            'body': 'Operator message',
            'author_id': self.operator.id,
        })

        self.assertFalse(self.session._should_trigger_n8n_webhook(message))


@tagged('post_install', '-at_install')
class TestWebhookController(TransactionCase):
    """Test cases for webhook controller."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Create livechat channel with API key
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Test Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'https://example.com/webhook',
        })
        cls.api_key = cls.channel.n8n_api_key

        # Create test session
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Test Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.channel.id,
            'uuid': 'abc-123-def-456-ghi',
        })

    def test_webhook_log_default_fields(self):
        """Test webhook log creation with default values."""
        log = self.env['n8n.webhook.log'].create({
            'event_type': 'inbound',
            'status': 'success',
        })

        # Timestamp should be auto-set
        self.assertIsNotNone(log.timestamp)
        self.assertEqual(log.event_type, 'inbound')
        self.assertEqual(log.status, 'success')

    def test_webhook_log_with_all_fields(self):
        """Test webhook log creation with all fields."""
        log = self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'livechat_channel_id': self.channel.id,
            'session_id': self.session.id,
            'status': 'failed',
            'response_time': 5432.1,
            'http_status': 500,
            'request_payload': '{"test": "request"}',
            'response_payload': '{"error": "Internal server error"}',
            'error_message': 'Connection refused',
        })

        self.assertEqual(log.livechat_channel_id, self.channel)
        self.assertEqual(log.session_id, self.session)
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.response_time, 5432.1)
        self.assertEqual(log.http_status, 500)
        self.assertIn('test', log.request_payload)
        self.assertIn('error', log.response_payload)
        self.assertEqual(log.error_message, 'Connection refused')

    def test_webhook_log_ordering(self):
        """Test webhook logs are ordered by timestamp descending."""
        # Create logs with different timestamps
        log1 = self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(hours=2),
        })
        log2 = self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(hours=1),
        })
        log3 = self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'status': 'success',
            'timestamp': fields.Datetime.now(),
        })

        # Search all logs
        logs = self.env['n8n.webhook.log'].search([
            ('id', 'in', [log1.id, log2.id, log3.id])
        ])

        # Should be ordered newest first
        self.assertEqual(logs[0], log3)
        self.assertEqual(logs[1], log2)
        self.assertEqual(logs[2], log1)

    def test_cleanup_logs_no_old_logs(self):
        """Test cleanup when there are no old logs."""
        # Create recent log
        self.env['n8n.webhook.log'].create({
            'event_type': 'outbound',
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=15),
        })

        # Run cleanup
        deleted_count = self.env['n8n.webhook.log']._cleanup_old_logs()

        # Should delete nothing
        self.assertEqual(deleted_count, 0)

    def test_cleanup_logs_multiple_old_logs(self):
        """Test cleanup deletes multiple old logs."""
        # Create multiple old logs
        for i in range(5):
            self.env['n8n.webhook.log'].create({
                'event_type': 'outbound',
                'status': 'success',
                'timestamp': fields.Datetime.now() - timedelta(days=35 + i),
            })

        # Run cleanup
        deleted_count = self.env['n8n.webhook.log']._cleanup_old_logs()

        # Should delete all 5
        self.assertEqual(deleted_count, 5)
