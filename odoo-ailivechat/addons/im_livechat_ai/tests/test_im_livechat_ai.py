# -*- coding: utf-8 -*-

import json
from datetime import timedelta
from unittest.mock import patch, MagicMock

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestImLivechatChannel(TransactionCase):
    """Tests for im_livechat.channel AI integration fields and methods."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Test AI Channel',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test-key-12345',
            'ai_model': 'gpt-4o',
            'ai_system_prompt': 'You are a helpful assistant.',
        })

    # --- Field Validation Tests ---

    def test_api_base_url_valid_https(self):
        """Valid HTTPS URL should pass validation."""
        self.channel.write({'ai_api_base_url': 'https://api.example.com/v1'})
        self.assertEqual(self.channel.ai_api_base_url, 'https://api.example.com/v1')

    def test_api_base_url_valid_http(self):
        """Valid HTTP URL should pass validation."""
        self.channel.write({'ai_api_base_url': 'http://localhost:8080/v1'})
        self.assertEqual(self.channel.ai_api_base_url, 'http://localhost:8080/v1')

    def test_api_base_url_invalid(self):
        """Invalid URL should raise UserError."""
        with self.assertRaises(UserError):
            self.channel.write({'ai_api_base_url': 'ftp://invalid.com'})

    def test_max_history_valid(self):
        """Valid max_history should pass."""
        self.channel.write({'ai_max_history': 100})
        self.assertEqual(self.channel.ai_max_history, 100)

    def test_max_history_too_low(self):
        """Max history below 1 should raise UserError."""
        with self.assertRaises(UserError):
            self.channel.write({'ai_max_history': 0})

    def test_max_history_too_high(self):
        """Max history above 200 should raise UserError."""
        with self.assertRaises(UserError):
            self.channel.write({'ai_max_history': 201})

    def test_temperature_valid(self):
        """Valid temperature should pass."""
        self.channel.write({'ai_temperature': 1.5})
        self.assertAlmostEqual(self.channel.ai_temperature, 1.5)

    def test_temperature_too_high(self):
        """Temperature above 2.0 should raise UserError."""
        with self.assertRaises(UserError):
            self.channel.write({'ai_temperature': 2.5})

    def test_temperature_negative(self):
        """Negative temperature should raise UserError."""
        with self.assertRaises(UserError):
            self.channel.write({'ai_temperature': -0.1})

    def test_default_values(self):
        """Default values should be set correctly."""
        channel = self.env['im_livechat.channel'].create({'name': 'Defaults Test'})
        self.assertFalse(channel.ai_enabled)
        self.assertEqual(channel.ai_max_history, 50)
        self.assertAlmostEqual(channel.ai_temperature, 0.7)
        self.assertEqual(channel.ai_max_tokens, 1024)
        self.assertEqual(channel.ai_max_retries, 3)
        self.assertEqual(channel.ai_retry_delay, 2)
        self.assertEqual(channel.ai_bot_name, 'AI Assistant')

    # --- Bot Partner Tests ---

    def test_get_or_create_bot_partner_creates_new(self):
        """Should create a new partner when none exists."""
        self.assertFalse(self.channel.ai_bot_partner_id)
        partner = self.channel._get_or_create_bot_partner()
        self.assertTrue(partner.exists())
        self.assertEqual(partner.name, 'AI Assistant')
        self.assertEqual(self.channel.ai_bot_partner_id.id, partner.id)

    def test_get_or_create_bot_partner_returns_existing(self):
        """Should return existing partner on subsequent calls."""
        partner1 = self.channel._get_or_create_bot_partner()
        partner2 = self.channel._get_or_create_bot_partner()
        self.assertEqual(partner1.id, partner2.id)

    def test_get_or_create_bot_partner_updates_name(self):
        """Should update partner name when bot name changes."""
        self.channel._get_or_create_bot_partner()
        self.channel.write({'ai_bot_name': 'Customer Service Bot'})
        partner = self.channel._get_or_create_bot_partner()
        self.assertEqual(partner.name, 'Customer Service Bot')

    # --- API Call Tests (mocked) ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_llm_api_success(self, mock_post):
        """Successful API call should return response data."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'Hello!'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        messages = [{'role': 'user', 'content': 'Hi'}]
        result = self.channel._call_llm_api(messages)

        self.assertEqual(result['choices'][0]['message']['content'], 'Hello!')
        self.assertEqual(result['usage']['total_tokens'], 15)

        # Verify correct URL construction
        call_args = mock_post.call_args
        self.assertTrue(call_args[0][0].endswith('/chat/completions'))

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_llm_api_url_construction(self, mock_post):
        """API URL should append /chat/completions if not present."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'OK'}}],
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        called_url = mock_post.call_args[0][0]
        self.assertEqual(called_url, 'https://api.openai.com/v1/chat/completions')

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_llm_api_url_no_double_path(self, mock_post):
        """Should not double-append /chat/completions."""
        self.channel.write({'ai_api_base_url': 'https://api.example.com/v1/chat/completions'})

        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'OK'}}],
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        called_url = mock_post.call_args[0][0]
        self.assertEqual(called_url, 'https://api.example.com/v1/chat/completions')

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_llm_api_empty_choices(self, mock_post):
        """Empty choices should raise ValueError."""
        mock_response = MagicMock()
        mock_response.json.return_value = {'choices': []}
        mock_response.text = '{"choices": []}'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        with self.assertRaises(ValueError):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_call_llm_api_sends_correct_payload(self, mock_post):
        """Should send correct model, messages, temperature, max_tokens."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'OK'}}],
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        messages = [{'role': 'system', 'content': 'Be helpful'}, {'role': 'user', 'content': 'Hi'}]
        self.channel._call_llm_api(messages)

        call_kwargs = mock_post.call_args
        sent_payload = call_kwargs[1]['json']
        self.assertEqual(sent_payload['model'], 'gpt-4o')
        self.assertEqual(sent_payload['messages'], messages)
        self.assertAlmostEqual(sent_payload['temperature'], 0.7)
        self.assertEqual(sent_payload['max_tokens'], 1024)

    # --- Test Connection Action ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_action_test_connection_success(self, mock_post):
        """Successful test should return success notification."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'OK'}}],
            'usage': {'total_tokens': 10},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')

    def test_action_test_connection_disabled(self):
        """Testing with AI disabled should raise UserError."""
        self.channel.write({'ai_enabled': False})
        with self.assertRaises(UserError):
            self.channel.action_test_ai_connection()
        # Restore
        self.channel.write({'ai_enabled': True})

    def test_action_test_connection_no_config(self):
        """Enabling AI without required fields should raise UserError from constraint."""
        with self.assertRaises(UserError):
            # Constraint fires when ai_enabled is True and ai_api_key is empty
            self.channel.write({'ai_api_key': False})

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
        """Should delete logs older than 30 days."""
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
        """Log exactly 30 days old should NOT be deleted (boundary test)."""
        boundary_log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=30),
        })

        count = self.env['llm.api.log']._cleanup_old_logs()

        # Exactly 30 days is NOT older than 30 days
        self.assertTrue(boundary_log.exists())

    def test_cleanup_returns_count(self):
        """Cleanup should return the number of deleted records."""
        for i in range(5):
            self.env['llm.api.log'].create({
                'status': 'success',
                'timestamp': fields.Datetime.now() - timedelta(days=31 + i),
            })

        count = self.env['llm.api.log']._cleanup_old_logs()
        self.assertEqual(count, 5)
