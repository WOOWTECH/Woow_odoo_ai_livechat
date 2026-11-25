# -*- coding: utf-8 -*-

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from .common import TEST_BACKEND, TEST_BASE_URL, call_backend, ok_response
from .common import make_ai_channel, set_ai


class TestImLivechatChannel(TransactionCase):
    """Tests for im_livechat.channel AI integration fields and methods."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = make_ai_channel(cls.env, {
            **TEST_BACKEND,
            'name': 'Test AI Channel',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
            'ai_system_prompt': 'You are a helpful assistant.',
        })

    # --- Field Validation Tests ---

    def test_max_history_valid(self):
        """Valid max_history should pass."""
        set_ai(self.channel, {'ai_max_history': 100})
        self.assertEqual(self.channel.sudo().ai_assistant_id.max_history, 100)

    def test_max_history_too_low(self):
        """Max history below 1 should raise UserError."""
        with self.assertRaises(UserError):
            set_ai(self.channel, {'ai_max_history': 0})

    def test_max_history_too_high(self):
        """Max history above 200 should raise UserError."""
        with self.assertRaises(UserError):
            set_ai(self.channel, {'ai_max_history': 201})

    def test_default_values(self):
        """Default values should be set correctly."""
        channel = self.env['im_livechat.channel'].create({'name': 'Defaults Test'})
        self.assertFalse(channel.ai_enabled)
        self.assertFalse(channel.ai_assistant_id)
        self.assertEqual(channel.ai_state, 'off')
        self.assertFalse(channel._ai_connection_is_ready())
        connection = self.env['im_livechat.ai.connection'].create({'name': 'Defaults'})
        self.assertEqual(connection.backend_type, 'openai')
        self.assertEqual(connection.max_retries, 1)
        self.assertEqual(connection.timeout_seconds, 45)
        self.assertEqual(connection.document_mode, 'file')
        self.assertFalse(connection.log_payload)
        self.assertTrue(connection.customer_safe)
        self.assertFalse(connection.model)
        self.assertFalse(connection.endpoint_url)
        self.assertFalse(connection.api_key_set)
        self.assertEqual(connection.readiness, 'blocked')
        assistant = self.env['im_livechat.ai.assistant'].create({'name': 'A', 'connection_id': connection.id})
        self.assertEqual(assistant.max_history, 50)
        self.assertFalse(assistant.livechat_available)
        for removed in ('ai_temperature', 'ai_max_tokens', 'ai_product_lookup', 'ai_model', 'ai_endpoint_url',
                        'ai_system_prompt', 'ai_bot_partner_id', 'ai_log_retention_days'):
            self.assertNotIn(removed, channel._fields)

    # --- Bot Partner Tests ---

    def test_get_or_create_bot_partner_creates_new(self):
        """The partner is created with the AI assistant, not on first reply."""
        partner = self.channel._get_bot_partner()
        self.assertTrue(partner.exists())
        self.assertEqual(partner.name, 'AI 客服助理')
        self.assertEqual(self.channel._get_bot_partner(), partner)
        self.assertEqual(partner, self.channel.sudo().ai_assistant_id.partner_id)

    def test_get_or_create_bot_partner_returns_existing(self):
        """Should return existing partner on subsequent calls."""
        partner1 = self.channel._get_bot_partner()
        partner2 = self.channel._get_bot_partner()
        self.assertEqual(partner1.id, partner2.id)

    def test_get_or_create_bot_partner_updates_name(self):
        """Should update partner name when bot name changes."""
        self.channel._get_bot_partner()
        set_ai(self.channel, {'ai_bot_name': 'Customer Service Bot'})
        partner = self.channel._get_bot_partner()
        self.assertEqual(partner.name, 'Customer Service Bot')

    # --- API Call Tests (mocked) ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_backend_success(self, mock_post):
        """A successful call returns the reply text."""
        mock_post.return_value = ok_response('Hello!')
        result = call_backend(self.channel, 'Hi')
        self.assertEqual(result['replies'], ['Hello!'])
        self.assertEqual(result['reply'], 'Hello!')
        self.assertFalse(result['error'])
        self.assertTrue(mock_post.call_args[0][0].endswith('/chat/completions'))

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_backend_url_construction(self, mock_post):
        """/chat/completions is appended to the base URL."""
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        self.assertEqual(mock_post.call_args[0][0], TEST_BASE_URL + '/chat/completions')

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_backend_url_no_double_path(self, mock_post):
        """A pasted /chat/completions is not appended twice."""
        set_ai(self.channel, {'ai_endpoint_url': 'https://api.example.com/v1/chat/completions/',
                              'ai_api_token': 'dummy-other'})
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        self.assertEqual(mock_post.call_args[0][0], 'https://api.example.com/v1/chat/completions')

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_backend_empty_choices(self, mock_post, mock_sleep):
        """Empty choices is an invalid response (an error, no reply)."""
        response = ok_response()
        response.json.return_value = {'choices': []}
        mock_post.return_value = response
        result = call_backend(self.channel)
        self.assertFalse(result['replies'])
        self.assertIn('the answer has an invalid format', result['error'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_backend_sends_correct_payload(self, mock_post):
        """model, messages, user, metadata; no temperature, max_tokens or tools."""
        mock_post.return_value = ok_response()
        call_backend(self.channel, 'Hi')
        sent_payload = mock_post.call_args[1]['json']
        self.assertEqual(sent_payload['model'], 'gpt-4o')
        self.assertEqual(sent_payload['messages'], [
            {'role': 'system', 'content': 'You are a helpful assistant.'},
            {'role': 'user', 'content': 'Hi'},
        ])
        for key in ('temperature', 'max_tokens', 'tools', 'tool_choice', 'stream'):
            self.assertNotIn(key, sent_payload)

    # --- Test Connection Action ---

    @patch('odoo.addons.im_livechat_ai.models.ai_connection.requests.get')
    def test_action_test_connection_success(self, mock_get):
        """Successful test should return success notification."""
        mock_get.return_value = ok_response()
        mock_get.return_value.json.return_value = {'data': [{'id': 'gpt-4o'}]}
        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')

    @patch('odoo.addons.im_livechat_ai.models.ai_connection.requests.get')
    def test_action_test_connection_works_with_ai_disabled(self, mock_get):
        """The administrator tests the connection before enabling AI."""
        self.channel.write({'ai_enabled': False})
        mock_get.return_value = ok_response()
        mock_get.return_value.json.return_value = {'data': [{'id': 'gpt-4o'}]}
        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')

    def test_action_test_connection_no_config(self):
        """Without URL the test refuses; an AI-enabled channel cannot lose its URL."""
        channel = self.env['im_livechat.channel'].create({'name': 'No Connection'})
        with self.assertRaises(UserError):
            channel.action_test_ai_connection()
        with self.assertRaises(UserError):
            set_ai(self.channel, {'ai_endpoint_url': False})

    # --- View Action Tests ---

    def test_action_view_api_logs(self):
        """Should return correct action definition."""
        result = self.channel.action_view_api_logs()
        self.assertEqual(result['res_model'], 'llm.api.log')
        self.assertEqual(result['domain'], [('livechat_channel_id', '=', self.channel.id)])


class TestLLMApiLog(TransactionCase):
    """Tests for llm.api.log model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Log Test Channel',
        })

    def test_log_creation(self):
        """Should create log with all fields."""
        log = self.env['llm.api.log'].create({
            'livechat_channel_id': self.channel.id,
            'model': 'gpt-4o',
            'status': 'success',
            'prompt_tokens': 100,
            'completion_tokens': 50,
            'total_tokens': 150,
            'response_time': 1.5,
            'request_payload': '{"messages": []}',
            'response_payload': '{"choices": []}',
        })
        self.assertTrue(log.exists())
        self.assertEqual(log.model, 'gpt-4o')
        self.assertEqual(log.total_tokens, 150)

    def test_log_default_timestamp(self):
        """Timestamp should default to now."""
        log = self.env['llm.api.log'].create({
            'status': 'success',
        })
        self.assertTrue(log.timestamp)

    def test_log_ordering(self):
        """Logs should be ordered by timestamp descending."""
        log1 = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(hours=1),
        })
        log2 = self.env['llm.api.log'].create({
            'status': 'error',
            'timestamp': fields.Datetime.now(),
        })
        logs = self.env['llm.api.log'].search([('id', 'in', [log1.id, log2.id])])
        self.assertEqual(logs[0].id, log2.id)

    def test_cleanup_old_logs(self):
        """Should delete logs older than the retention (30 days)."""
        old_log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=31),
        })
        recent_log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=1),
        })

        self.env['llm.api.log']._cleanup_old_logs()

        self.assertFalse(old_log.exists())
        self.assertTrue(recent_log.exists())

    def test_cleanup_boundary_30_days(self):
        """Retention is 30 days: just inside is kept, just outside is deleted."""
        inside_log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=30) + timedelta(minutes=5),
        })
        outside_log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=30) - timedelta(minutes=5),
        })

        self.env['llm.api.log']._cleanup_old_logs()

        self.assertTrue(inside_log.exists())
        self.assertFalse(outside_log.exists())

    def test_cleanup_returns_count(self):
        """Cleanup should return the number of deleted records."""
        for i in range(5):
            self.env['llm.api.log'].create({
                'status': 'success',
                'timestamp': fields.Datetime.now() - timedelta(days=31 + i),
            })

        count = self.env['llm.api.log']._cleanup_old_logs()
        self.assertEqual(count, 5)
