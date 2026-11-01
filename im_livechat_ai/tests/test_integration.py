# -*- coding: utf-8 -*-

from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase


from .common import TEST_BASE_URL, livechat_operator, run_ai_job, TEST_BACKEND


class TestAIIntegration(TransactionCase):
    """Integration tests for the full AI livechat flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.livechat_channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Integration Test Channel',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
            'ai_system_prompt': 'You are a test bot.',
            'ai_max_retries': 2,
            'ai_error_message': 'AI is unavailable.',
        })

        cls.bot_partner = cls.livechat_channel._get_or_create_bot_partner()

        cls.visitor_partner = cls.env['res.partner'].create({
            'name': 'Integration Visitor',
        })

        cls.session = cls.env['discuss.channel'].create({
            'name': 'Integration Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.livechat_channel.id,
        })
        # A visitor message must exist, otherwise the processor waits for a
        # commit (cr.rollback), which is forbidden inside a test.
        cls._add_visitor_msg(cls.session, cls.visitor_partner)

    @classmethod
    def _add_visitor_msg(cls, session, partner, body='Hello, I need help'):
        return cls.env['mail.message'].create({
            'body': body,
            'model': 'discuss.channel',
            'res_id': session.id,
            'message_type': 'comment',
            'author_id': partner.id,
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
        run_ai_job(self.env, self.session)

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

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_full_flow_api_failure_sends_error_message(self, mock_post, mock_sleep):
        """When API fails all retries, error message should be sent to visitor."""
        mock_post.side_effect = Exception("API connection failed")

        run_ai_job(self.env, self.session)

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

        run_ai_job(self.env, self.session)

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

        # Verify the 3 s / 10 s backoff between retries
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [3, 10])

    # --- Multi-Channel Isolation Tests ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_multi_channel_isolation(self, mock_post):
        """Each channel uses its own backend connection and model."""
        channel_b = self.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'ai_endpoint_url': 'https://other-gateway.test/v1',
            'ai_api_token': 'dummy-other-token',
            'name': 'Channel B',
            'ai_enabled': True,
            'ai_model': 'other-pool/other-model',
            'ai_system_prompt': 'You are MiniMax bot.',
        })

        mock_response = MagicMock()

        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'MiniMax reply'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        session_b = self.env['discuss.channel'].create({
            'name': 'Session B',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(self.env).id,
            'livechat_channel_id': channel_b.id,
        })
        self._add_visitor_msg(session_b, self.visitor_partner)

        run_ai_job(self.env, session_b)

        # The channel's own endpoint and token
        called_url = mock_post.call_args[0][0]
        self.assertEqual(called_url, 'https://other-gateway.test/v1/chat/completions')
        self.assertEqual(mock_post.call_args[1]['headers']['Authorization'], 'Bearer dummy-other-token')
        self.assertNotEqual(called_url, TEST_BASE_URL + '/chat/completions')

        # Channel-specific model was sent
        sent_payload = mock_post.call_args[1]['json']
        self.assertEqual(sent_payload['model'], 'other-pool/other-model')

    def test_disabled_channel_no_trigger(self):
        """Disabled channel should not trigger AI response."""
        self.livechat_channel.write({'ai_enabled': False})

        # _trigger_ai_response should return early without queuing a job
        job = self.livechat_channel._trigger_ai_response(self.session, MagicMock())
        self.assertFalse(job)

        # Restore
        self.livechat_channel.write({'ai_enabled': True})

    def test_missing_config_constraint(self):
        """Enabling AI on a channel without a backend connection raises an error."""
        from odoo.exceptions import UserError
        channel = self.env['im_livechat.channel'].create({'name': 'No Backend', 'ai_model': 'gpt-4o'})
        with self.assertRaises(UserError):
            channel.write({'ai_enabled': True})

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
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'Response'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        run_ai_job(self.env, self.session)

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
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'Quick response'}}],
            'usage': {},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        run_ai_job(self.env, self.session)

        log = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.livechat_channel.id),
            ('status', '=', 'success'),
        ], limit=1, order='id desc')

        self.assertIsNotNone(log.response_time)
        self.assertGreater(log.response_time, 0)
