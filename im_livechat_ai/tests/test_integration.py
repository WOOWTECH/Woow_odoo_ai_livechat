# -*- coding: utf-8 -*-

from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase


class TestAIIntegration(TransactionCase):
    """Integration tests for the full AI livechat flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.livechat_channel = cls.env['im_livechat.channel'].create({
            'name': 'Integration Test Channel',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-integration-test',
            'ai_model': 'gpt-4o',
            'ai_system_prompt': 'You are a test bot.',
            'ai_max_retries': 3,
            'ai_retry_delay': 1,
            'ai_error_message': 'AI is unavailable.',
        })

        cls.bot_partner = cls.livechat_channel._get_or_create_bot_partner()

        cls.visitor_partner = cls.env['res.partner'].create({
            'name': 'Integration Visitor',
        })

        cls.session = cls.env['discuss.channel'].create({
            'name': 'Integration Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.livechat_channel.id,
        })

    # --- Full Flow Tests ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_full_flow_success(self, mock_post):
        """Full flow: visitor message → AI API call → bot reply → log created."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'Hello! How can I help?'}}],
            'usage': {'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        # Use test_env to avoid dual-cursor issue in TransactionCase
        self.livechat_channel._process_ai_response(
            self.env.cr.dbname,
            self.livechat_channel.id,
            self.session.id,
            test_env=self.env,
        )

        # Verify bot message was posted
        bot_messages = self.env['mail.message'].search([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
            ('author_id', '=', self.bot_partner.id),
        ])
        self.assertTrue(len(bot_messages) >= 1)
        self.assertIn('Hello! How can I help?', bot_messages[0].body)

        # Verify log was created
        logs = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.livechat_channel.id),
            ('status', '=', 'success'),
        ])
        self.assertTrue(len(logs) >= 1)
        self.assertEqual(logs[0].total_tokens, 60)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_full_flow_api_failure_sends_error_message(self, mock_post):
        """When API fails all retries, error message should be sent to visitor."""
        mock_post.side_effect = Exception("API connection failed")

        self.livechat_channel._process_ai_response(
            self.env.cr.dbname,
            self.livechat_channel.id,
            self.session.id,
            test_env=self.env,
        )

        # Verify error message was posted
        bot_messages = self.env['mail.message'].search([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
            ('author_id', '=', self.bot_partner.id),
        ])
        error_msgs = [m for m in bot_messages if 'AI is unavailable' in (m.body or '')]
        self.assertTrue(len(error_msgs) >= 1)

        # Verify error log was created
        error_logs = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.livechat_channel.id),
            ('status', '=', 'error'),
        ])
        self.assertTrue(len(error_logs) >= 1)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    def test_retry_then_success(self, mock_sleep, mock_post):
        """Should succeed after failed retries."""
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {
            'choices': [{'message': {'content': 'Success after retry!'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }
        success_response.raise_for_status = MagicMock()

        # Fail first 2 times, succeed on 3rd
        mock_post.side_effect = [
            Exception("Error 1"),
            Exception("Error 2"),
            success_response,
        ]

        self.livechat_channel._process_ai_response(
            self.env.cr.dbname,
            self.livechat_channel.id,
            self.session.id,
            test_env=self.env,
        )

        # Verify success message was posted
        bot_messages = self.env['mail.message'].search([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
            ('author_id', '=', self.bot_partner.id),
        ])
        success_msgs = [m for m in bot_messages if 'Success after retry!' in (m.body or '')]
        self.assertTrue(len(success_msgs) >= 1)

        # Verify retry logs exist
        retry_logs = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.livechat_channel.id),
            ('status', '=', 'retry'),
        ])
        self.assertTrue(len(retry_logs) >= 2)

        # Verify sleep was called between retries
        self.assertEqual(mock_sleep.call_count, 2)

    # --- Multi-Channel Isolation Tests ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_multi_channel_isolation(self, mock_post):
        """Each channel should use its own API config."""
        channel_b = self.env['im_livechat.channel'].create({
            'name': 'Channel B',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.minimax.com/v1',
            'ai_api_key': 'sk-minimax-key',
            'ai_model': 'MiniMax-Text-01',
            'ai_system_prompt': 'You are MiniMax bot.',
        })

        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'MiniMax reply'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        session_b = self.env['discuss.channel'].create({
            'name': 'Session B',
            'channel_type': 'livechat',
            'livechat_channel_id': channel_b.id,
        })

        channel_b._process_ai_response(
            self.env.cr.dbname,
            channel_b.id,
            session_b.id,
            test_env=self.env,
        )

        # Verify correct URL was called
        called_url = mock_post.call_args[0][0]
        self.assertIn('minimax', called_url)

        # Verify correct model was sent
        sent_payload = mock_post.call_args[1]['json']
        self.assertEqual(sent_payload['model'], 'MiniMax-Text-01')

    def test_disabled_channel_no_trigger(self):
        """Disabled channel should not trigger AI response."""
        self.livechat_channel.write({'ai_enabled': False})

        # _trigger_ai_response should return early
        self.livechat_channel._trigger_ai_response(self.session, MagicMock())

        # Restore
        self.livechat_channel.write({'ai_enabled': True})

    def test_missing_config_constraint(self):
        """Enabling AI without required config should raise constraint error."""
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.livechat_channel.write({'ai_api_key': False})

    # --- Bot Partner Isolation ---

    def test_each_channel_has_own_bot(self):
        """Each channel should create its own bot partner."""
        channel_a = self.env['im_livechat.channel'].create({
            'name': 'Channel A Bot Test',
            'ai_bot_name': 'Bot Alpha',
        })
        channel_b = self.env['im_livechat.channel'].create({
            'name': 'Channel B Bot Test',
            'ai_bot_name': 'Bot Beta',
        })

        bot_a = channel_a._get_or_create_bot_partner()
        bot_b = channel_b._get_or_create_bot_partner()

        self.assertNotEqual(bot_a.id, bot_b.id)
        self.assertEqual(bot_a.name, 'Bot Alpha')
        self.assertEqual(bot_b.name, 'Bot Beta')

    # --- Log Creation Tests ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_log_records_token_usage(self, mock_post):
        """Logs should record token usage from API response."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'Response'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.livechat_channel._process_ai_response(
            self.env.cr.dbname,
            self.livechat_channel.id,
            self.session.id,
            test_env=self.env,
        )

        log = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.livechat_channel.id),
            ('status', '=', 'success'),
        ], limit=1, order='id desc')

        self.assertEqual(log.prompt_tokens, 100)
        self.assertEqual(log.completion_tokens, 50)
        self.assertEqual(log.total_tokens, 150)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_log_records_response_time(self, mock_post):
        """Logs should record response time."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'Quick response'}}],
            'usage': {},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.livechat_channel._process_ai_response(
            self.env.cr.dbname,
            self.livechat_channel.id,
            self.session.id,
            test_env=self.env,
        )

        log = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.livechat_channel.id),
            ('status', '=', 'success'),
        ], limit=1, order='id desc')

        self.assertIsNotNone(log.response_time)
        self.assertGreater(log.response_time, 0)
