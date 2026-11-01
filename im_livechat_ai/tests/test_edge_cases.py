# -*- coding: utf-8 -*-
"""
Edge-case unit tests for im_livechat_ai module.

Covers boundary conditions, adversarial inputs, security, concurrency,
and failure modes not covered by the existing test suite.
"""

from datetime import timedelta
from unittest.mock import patch, MagicMock

import requests

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    LLM_REQUEST_TIMEOUT,
    MAX_LOG_ERROR_LENGTH,
)

from .common import TEST_API_KEY, TEST_BACKEND, call_backend, livechat_operator, ok_response, run_ai_job


class TestStripThinkTags(TransactionCase):
    """Exhaustive tests for _strip_think_tags static method."""

    def _strip(self, text):
        return self.env['im_livechat.channel']._strip_think_tags(text)

    # --- None / Empty / Falsy ---

    def test_none_input(self):
        self.assertEqual(self._strip(None), '')

    def test_empty_string(self):
        self.assertEqual(self._strip(''), '')

    def test_false_input(self):
        self.assertFalse(self._strip(False))

    def test_zero_input(self):
        self.assertEqual(self._strip(0), '')

    # --- No tags ---

    def test_no_tags_plain_text(self):
        self.assertEqual(self._strip('Hello world'), 'Hello world')

    def test_no_tags_with_angle_brackets(self):
        self.assertEqual(self._strip('5 < 10 and 10 > 5'), '5 < 10 and 10 > 5')

    # --- Complete paired tags ---

    def test_single_think_block(self):
        self.assertEqual(self._strip('<think>reasoning</think>Hello'), 'Hello')

    def test_think_block_at_end(self):
        self.assertEqual(self._strip('Hello<think>reasoning</think>'), 'Hello')

    def test_think_block_in_middle(self):
        self.assertEqual(self._strip('Start<think>reasoning</think>End'), 'StartEnd')

    def test_multiple_think_blocks(self):
        self.assertEqual(
            self._strip('<think>first</think>A<think>second</think>B'),
            'AB',
        )

    def test_empty_think_tags(self):
        self.assertEqual(self._strip('<think></think>Hello'), 'Hello')

    # --- Multiline content ---

    def test_multiline_think_block(self):
        text = '<think>\nline1\nline2\nline3\n</think>Response'
        self.assertEqual(self._strip(text), 'Response')

    def test_multiline_response_preserved(self):
        text = '<think>reasoning</think>Line 1\nLine 2\nLine 3'
        result = self._strip(text)
        self.assertIn('Line 1', result)
        self.assertIn('Line 2', result)
        self.assertIn('Line 3', result)

    # --- Orphaned opening tag (truncated response) ---

    def test_orphaned_opening_tag(self):
        """Response cut off mid-think block."""
        self.assertEqual(self._strip('Hello<think>still reasoning...'), 'Hello')

    def test_orphaned_opening_tag_at_start(self):
        self.assertEqual(self._strip('<think>still reasoning...'), '')

    def test_orphaned_opening_tag_after_complete(self):
        text = '<think>done</think>Hello<think>incomplete'
        self.assertEqual(self._strip(text), 'Hello')

    # --- Whitespace handling ---

    def test_strips_leading_trailing_whitespace(self):
        self.assertEqual(self._strip('<think>x</think>  Hello  '), 'Hello')

    def test_only_think_tags_results_in_empty(self):
        self.assertEqual(self._strip('<think>just reasoning</think>'), '')

    def test_whitespace_only_after_stripping(self):
        self.assertEqual(self._strip('<think>reasoning</think>   '), '')

    # --- Special content inside think tags ---

    def test_html_inside_think(self):
        text = '<think><b>bold reasoning</b></think>Response'
        self.assertEqual(self._strip(text), 'Response')

    def test_markdown_inside_think(self):
        text = '<think>**bold** `code`</think>Response'
        self.assertEqual(self._strip(text), 'Response')

    def test_unicode_inside_think(self):
        text = '<think>中文推理內容</think>回應'
        self.assertEqual(self._strip(text), '回應')

    def test_special_chars_inside_think(self):
        text = '<think>@#$%^&*()!~`</think>Response'
        self.assertEqual(self._strip(text), 'Response')

    # --- Nested / malformed tags ---

    def test_nested_think_tags(self):
        """Nested tags should all be removed."""
        text = '<think>outer<think>inner</think>still outer</think>Response'
        result = self._strip(text)
        self.assertNotIn('<think>', result)
        self.assertIn('Response', result)

    def test_wrong_closing_tag(self):
        """Malformed closing tag should leave content intact."""
        text = '<think>reasoning</thunk>Response'
        result = self._strip(text)
        # Orphaned opening tag regex should catch this
        self.assertEqual(result, '')  # <think> matches to end of string

    def test_case_insensitive_and_thinking_tags(self):
        """<Think>, <thinking> and special tokens are removed too (same rules as
        woow_ai_contact)."""
        self.assertEqual(self._strip('<Think>reasoning</Think>Response'), 'Response')
        self.assertEqual(self._strip('<thinking>x</thinking>答案<|eot_id|>'), '答案')
        self.assertEqual(self._strip('reasoning only</think>答案'), '答案')

    # --- Very long content ---

    def test_very_long_think_block(self):
        reasoning = 'a' * 10000
        text = f'<think>{reasoning}</think>Short response'
        self.assertEqual(self._strip(text), 'Short response')

    def test_very_long_response_after_think(self):
        response = 'b' * 10000
        text = f'<think>short</think>{response}'
        self.assertEqual(self._strip(text), response)


class TestConstraintEdgeCases(TransactionCase):
    """Edge cases for field constraint validators."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Constraint Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
        })

    # --- Boundary values ---

    def test_max_history_exact_boundaries(self):
        """Exact boundary values 1 and 200 should be accepted."""
        self.channel.write({'ai_max_history': 1})
        self.assertEqual(self.channel.ai_max_history, 1)
        self.channel.write({'ai_max_history': 200})
        self.assertEqual(self.channel.ai_max_history, 200)

    def test_max_history_negative(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_max_history': -1})

    def test_max_retries_exact_boundaries(self):
        self.channel.write({'ai_max_retries': 0})
        self.assertEqual(self.channel.ai_max_retries, 0)
        self.channel.write({'ai_max_retries': 2})
        self.assertEqual(self.channel.ai_max_retries, 2)

    def test_max_retries_negative(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_max_retries': -1})

    def test_max_retries_three(self):
        """At most 2 retries (P1-7)."""
        with self.assertRaises(UserError):
            self.channel.write({'ai_max_retries': 3})

    def test_retry_delay_field_removed(self):
        """The backoff is fixed (3 s, 10 s); the per-channel delay is gone."""
        self.assertNotIn('ai_retry_delay', self.channel._fields)

    # --- Disabled AI bypasses range checks ---

    def test_disabled_ai_still_checks_max_history(self):
        """P1-1: range constraints also apply while AI is disabled."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'Disabled Test',
            'ai_enabled': False,
        })
        with self.assertRaises(ValidationError):
            channel.write({'ai_max_history': 0})

    # --- Required fields when enabling AI ---

    def test_enable_ai_without_token_fails(self):
        """Enabling AI needs the channel's access token."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'No Key', 'ai_endpoint_url': 'https://litellm.test/v1', 'ai_model': 'gpt-4o',
        })
        with self.assertRaises(UserError):
            channel.write({'ai_enabled': True})

    def test_enable_ai_without_url_fails(self):
        """There is no default URL any more (18.0.1.2.0)."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'No URL', 'ai_api_token': 'dummy', 'ai_model': 'gpt-4o',
        })
        with self.assertRaises(UserError):
            channel.write({'ai_enabled': True})

    def test_enable_ai_without_model_fails(self):
        channel = self.env['im_livechat.channel'].create({'name': 'No Model'})
        with self.assertRaises(UserError):
            channel.write({
                'ai_enabled': True,
                'ai_model': False,
            })


class TestCallLlmApiEdgeCases(TransactionCase):
    """Edge cases of one OpenAI compatible backend call (no retries here)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'API Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
            'ai_max_retries': 0,
        })

    # --- URL construction edge cases ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_url_with_trailing_slash(self, mock_post):
        """Trailing slash should be stripped before appending path."""
        self.channel.write({'ai_endpoint_url': 'https://api.openai.com/v1/'})
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        called_url = mock_post.call_args[0][0]
        self.assertEqual(called_url, 'https://api.openai.com/v1/chat/completions')
        self.assertNotIn('//', called_url.replace('https://', ''))

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_url_with_whitespace(self, mock_post):
        """Leading/trailing whitespace in URL is stripped on save."""
        self.channel.write({'ai_endpoint_url': '  https://api.openai.com/v1  '})
        self.assertEqual(self.channel.ai_endpoint_url, 'https://api.openai.com/v1')
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        self.assertEqual(mock_post.call_args[0][0], 'https://api.openai.com/v1/chat/completions')

    # --- Response format edge cases ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_no_choices_key(self, mock_post):
        """A response without choices is an invalid response."""
        response = ok_response()
        response.json.return_value = {'error': 'something'}
        mock_post.return_value = response
        result = call_backend(self.channel)
        self.assertFalse(result['replies'])
        self.assertIn('回應格式不正確', result['error'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_choices_is_none(self, mock_post):
        response = ok_response()
        response.json.return_value = {'choices': None}
        mock_post.return_value = response
        self.assertIn('回應格式不正確', call_backend(self.channel)['error'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_content_parts_list(self, mock_post):
        """content may be a list of text parts."""
        response = ok_response()
        response.json.return_value = {'choices': [{'message': {'content': [
            {'type': 'text', 'text': '第一段'}, {'type': 'image_url'}, {'type': 'text', 'text': '第二段'},
        ]}}]}
        mock_post.return_value = response
        self.assertEqual(call_backend(self.channel)['replies'], ['第一段\n第二段'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_not_json(self, mock_post):
        response = ok_response()
        response.json.side_effect = ValueError('not json')
        mock_post.return_value = response
        self.assertIn('回應格式不正確', call_backend(self.channel)['error'])

    # --- HTTP error codes ---

    def _http_error(self, mock_post, status):
        response = ok_response()
        response.status_code = status
        response.text = '{"error": "upstream detail"}'
        mock_post.return_value = response
        return call_backend(self.channel)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_401_unauthorized(self, mock_post):
        result = self._http_error(mock_post, 401)
        self.assertEqual(result['error'], 'HTTP 401: 金鑰或簽章無效 (HTTPError)')

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_429_rate_limited(self, mock_post):
        self.assertIn('HTTP 429', self._http_error(mock_post, 429)['error'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_500_server_error(self, mock_post):
        result = self._http_error(mock_post, 500)
        self.assertIn('HTTP 500', result['error'])
        self.assertNotIn('upstream detail', result['error'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_redirect_is_an_error(self, mock_post):
        """Redirects are not followed (the token would go elsewhere)."""
        result = self._http_error(mock_post, 302)
        self.assertIn('HTTP 302', result['error'])
        self.assertIs(mock_post.call_args[1]['allow_redirects'], False)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_connection_timeout(self, mock_post):
        mock_post.side_effect = requests.Timeout('Connection timed out')
        self.assertIn('連線逾時', call_backend(self.channel)['error'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_connection_refused(self, mock_post):
        mock_post.side_effect = requests.ConnectionError('Connection refused')
        self.assertIn('無法連線到 AI 後端', call_backend(self.channel)['error'])

    # --- Payload construction ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_no_temperature_max_tokens_or_tools(self, mock_post):
        """The backend decides: no temperature, max_tokens, tools or stream."""
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        payload = mock_post.call_args[1]['json']
        self.assertEqual(set(payload), {'model', 'messages', 'user', 'metadata'})

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_model_only_when_set(self, mock_post):
        """Hermes ignores the model: an empty model is not sent (AI off here,
        an enabled OpenAI channel needs one)."""
        self.channel.write({'ai_enabled': False})
        self.channel.write({'ai_model': False})
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        self.assertNotIn('model', mock_post.call_args[1]['json'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_authorization_header_format(self, mock_post):
        """Authorization header should be Bearer token format."""
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        headers = mock_post.call_args[1]['headers']
        self.assertEqual(headers['Authorization'], 'Bearer ' + TEST_API_KEY)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_request_timeout_constant(self, mock_post):
        """Request timeout should be the module's LLM_REQUEST_TIMEOUT."""
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        self.assertEqual(mock_post.call_args[1]['timeout'], LLM_REQUEST_TIMEOUT)


class TestCreateBotMessageEdgeCases(TransactionCase):
    """Edge cases for _create_bot_message — XSS prevention, line breaks."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Bot Msg Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Bot Msg Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.channel.id,
        })

    def _get_latest_bot_message(self):
        bot = self.channel._get_or_create_bot_partner()
        msgs = self.env['mail.message'].search([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
            ('author_id', '=', bot.id),
        ], order='id desc', limit=1)
        return msgs[0] if msgs else None

    # --- XSS prevention ---

    def test_script_tag_escaped(self):
        """<script> tags in LLM response should be escaped."""
        self.channel._create_bot_message(
            self.env, self.session,
            '<script>alert("xss")</script>Hello',
        )
        msg = self._get_latest_bot_message()
        self.assertNotIn('<script>', msg.body)
        self.assertIn('&lt;script&gt;', msg.body)

    def test_html_tags_escaped(self):
        """HTML tags in LLM response should be escaped."""
        self.channel._create_bot_message(
            self.env, self.session,
            '<img src=x onerror=alert(1)>Test',
        )
        msg = self._get_latest_bot_message()
        self.assertNotIn('<img', msg.body)
        self.assertIn('&lt;img', msg.body)

    def test_ampersand_escaped(self):
        """Ampersands should be escaped."""
        self.channel._create_bot_message(
            self.env, self.session,
            'A & B',
        )
        msg = self._get_latest_bot_message()
        self.assertIn('&amp;', msg.body)

    # --- Line break conversion ---

    def test_newlines_to_br(self):
        """Newlines should be converted to <br/>."""
        self.channel._create_bot_message(
            self.env, self.session,
            'Line 1\nLine 2\nLine 3',
        )
        msg = self._get_latest_bot_message()
        # message_post sanitizes <br/> to <br>
        self.assertEqual(msg.body.count('<br'), 2)

    def test_multiple_consecutive_newlines(self):
        """A blank line separates two paragraphs (markdown), no <br/>."""
        self.channel._create_bot_message(
            self.env, self.session,
            'Para 1\n\nPara 2',
        )
        msg = self._get_latest_bot_message()
        self.assertEqual(msg.body.count('<br'), 0)
        self.assertIn('<p>Para 1</p><p>Para 2</p>', msg.body)

    # --- Unicode content ---

    def test_chinese_content(self):
        """Chinese characters should be preserved."""
        self.channel._create_bot_message(
            self.env, self.session,
            '你好，歡迎使用我們的服務！',
        )
        msg = self._get_latest_bot_message()
        self.assertIn('你好', msg.body)

    def test_emoji_content(self):
        """Emoji should be preserved."""
        self.channel._create_bot_message(
            self.env, self.session,
            'Hello 👋🤖',
        )
        msg = self._get_latest_bot_message()
        self.assertIn('👋', msg.body)

    def test_mixed_unicode(self):
        """Mixed unicode scripts should be preserved."""
        self.channel._create_bot_message(
            self.env, self.session,
            'Hello مرحبا 你好 こんにちは',
        )
        msg = self._get_latest_bot_message()
        self.assertIn('مرحبا', msg.body)
        self.assertIn('你好', msg.body)
        self.assertIn('こんにちは', msg.body)


class TestBuildLlmMessagesEdgeCases(TransactionCase):
    """Edge cases for _build_llm_messages in discuss_channel.py."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Build Msg Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
            'ai_system_prompt': 'Test system prompt.',
            'ai_max_history': 50,
        })
        cls.bot_partner = cls.channel._get_or_create_bot_partner()
        cls.visitor = cls.env['res.partner'].create({'name': 'Edge Visitor'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Build Msg Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.channel.id,
        })

    def _post_msg(self, body, author_id=None, author_guest_id=None):
        vals = {
            'body': body,
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'comment',
            # mail.message defaults the author to the current user's partner;
            # anonymous/guest visitors have none.
            'author_id': False,
        }
        if author_id:
            vals['author_id'] = author_id
        if author_guest_id:
            vals['author_guest_id'] = author_guest_id
        return self.env['mail.message'].create(vals)

    # --- Empty channel ---

    def test_no_messages_returns_system_only(self):
        """Channel with no messages returns only the system prompt (no turns)."""
        session2 = self.env['discuss.channel'].create({
            'name': 'Empty Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(self.env).id,
            'livechat_channel_id': self.channel.id,
        })
        messages = session2._build_llm_messages(self.channel)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['role'], 'system')

    # --- HTML entity decoding ---

    def test_html_entities_decoded(self):
        """HTML entities like &amp; should be decoded to &."""
        self._post_msg('<p>A &amp; B</p>', author_id=self.visitor.id)
        messages = self.session._build_llm_messages(self.channel)
        user_msgs = [m for m in messages if m['role'] == 'user']
        self.assertTrue(any('A & B' in m['content'] for m in user_msgs))

    def test_nbsp_decoded(self):
        """&nbsp; should be decoded to space."""
        self._post_msg('<p>Hello&nbsp;World</p>', author_id=self.visitor.id)
        messages = self.session._build_llm_messages(self.channel)
        user_msgs = [m for m in messages if m['role'] == 'user']
        # Should have Hello and World as text content
        self.assertTrue(any('Hello' in m['content'] and 'World' in m['content'] for m in user_msgs))

    # --- Complex HTML ---

    def test_nested_html_stripped(self):
        """Deeply nested HTML should be fully stripped."""
        self._post_msg(
            '<div><p><strong><em>Deep text</em></strong></p></div>',
            author_id=self.visitor.id,
        )
        messages = self.session._build_llm_messages(self.channel)
        user_msgs = [m for m in messages if m['role'] == 'user']
        self.assertTrue(any('Deep text' in m['content'] for m in user_msgs))
        # No HTML tags should remain
        for m in user_msgs:
            if 'Deep text' in m['content']:
                self.assertNotIn('<', m['content'])

    def test_img_tag_stripped(self):
        """Image tags should be stripped, leaving no content."""
        self._post_msg('<img src="photo.jpg"/>', author_id=self.visitor.id)
        messages = self.session._build_llm_messages(self.channel)
        # Image-only message should be skipped (empty after strip)
        for m in messages:
            if m['role'] == 'user':
                self.assertNotIn('<img', m.get('content', ''))

    # --- Max history clamping ---

    def test_max_history_none_defaults_to_50(self):
        """ai_max_history=0 (legacy data only) falls back to 50 internally."""
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_max_history = 0 WHERE id = %s",
                            (self.channel.id,))
        self.env.invalidate_all()
        # _build_llm_messages clamps: max(1, min(0 or 50, 200)) = 50
        self._post_msg('Msg 1', author_id=self.visitor.id)
        self._post_msg('Msg 2', author_id=self.visitor.id)
        messages = self.session._build_llm_messages(self.channel)
        non_system = [m for m in messages if m['role'] != 'system']
        # Both messages fit in the 50-message fallback window
        self.assertEqual(len(non_system), 2)
        # Restore
        self.channel.write({'ai_max_history': 50})

    # --- Message type filtering ---

    def test_notification_messages_excluded(self):
        """notification type messages should not be included."""
        self.env['mail.message'].create({
            'body': 'System notification',
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'notification',
        })
        self._post_msg('Real message', author_id=self.visitor.id)
        messages = self.session._build_llm_messages(self.channel)
        for m in messages:
            if m['role'] == 'user':
                self.assertNotIn('System notification', m['content'])

    # --- Guest visitor detection ---

    def test_guest_message_is_user_role(self):
        """Guest-authored messages should have role 'user'."""
        guest = self.env['mail.guest'].create({'name': 'Test Guest'})
        self._post_msg('Guest message', author_guest_id=guest.id)
        messages = self.session._build_llm_messages(self.channel)
        user_msgs = [m for m in messages if m['role'] == 'user']
        self.assertTrue(any('Guest message' in m['content'] for m in user_msgs))


class TestDoProcessAiResponseEdgeCases(TransactionCase):
    """Edge cases for AI reply jobs — record deletion, empty responses."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Process Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
            'ai_max_retries': 2,
            'ai_error_message': 'AI unavailable.',
        })
        cls.bot_partner = cls.channel._get_or_create_bot_partner()
        cls.visitor = cls.env['res.partner'].create({'name': 'Process Visitor'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Process Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.channel.id,
        })

    def _add_visitor_msg(self, body='Hello'):
        self.env['mail.message'].create({
            'body': body,
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'comment',
            'author_id': self.visitor.id,
        })

    # --- Channel/session not found ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_channel_disabled_job_skipped(self, mock_post):
        """A job whose channel had AI turned off is skipped without an API call."""
        self._add_visitor_msg()
        self.channel.write({'ai_enabled': False})
        job = run_ai_job(self.env, self.session)
        mock_post.assert_not_called()
        self.assertEqual(job.state, 'skipped')
        self.channel.write({'ai_enabled': True})

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_session_deleted_job_gone(self, mock_post):
        """Deleting the session deletes its jobs; running a deleted job is a no-op."""
        session = self.env['discuss.channel'].create({
            'name': 'Doomed Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(self.env).id,
            'livechat_channel_id': self.channel.id,
        })
        msg = self.env['mail.message'].create({
            'body': 'hi', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': self.visitor.id,
        })
        job = self.env['im_livechat.ai.job']._create_for_message(session, msg)
        job_id = job.id
        session.unlink()
        self.assertFalse(self.env['im_livechat.ai.job'].browse(job_id).exists())
        self.assertEqual(self.env['im_livechat.ai.job'].browse(job_id)._run_job(commit=False), (None, False))
        mock_post.assert_not_called()

    # --- Empty AI response (only think tags) ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_only_think_tags_triggers_retry(self, mock_post, mock_sleep):
        """Response with only think tags (empty after strip) should retry."""
        self._add_visitor_msg()

        # First call returns only think tags, second returns real content
        response_think_only = MagicMock()
        response_think_only.status_code = 200
        response_think_only.json.return_value = {
            'choices': [{'message': {'content': '<think>just reasoning</think>'}}],
            'usage': {},
        }
        response_think_only.raise_for_status = MagicMock()

        response_real = MagicMock()

        response_real.status_code = 200
        response_real.status_code = 200
        response_real.json.return_value = {
            'choices': [{'message': {'content': '<think>reasoning</think>Real answer'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }
        response_real.raise_for_status = MagicMock()

        mock_post.side_effect = [response_think_only, response_real]

        run_ai_job(self.env, self.session)

        # Verify the real answer was posted (not empty)
        bot_msgs = self.env['mail.message'].search([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
            ('author_id', '=', self.bot_partner.id),
        ], order='id desc', limit=1)
        self.assertTrue(bot_msgs)
        self.assertIn('Real answer', bot_msgs[0].body)

    # --- All retries exhausted sends error message ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_all_retries_exhausted_sends_error(self, mock_post, mock_sleep):
        """When all retries fail, error message should be posted."""
        self._add_visitor_msg()
        mock_post.side_effect = Exception('API down')

        run_ai_job(self.env, self.session)

        bot_msgs = self.env['mail.message'].search([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
            ('author_id', '=', self.bot_partner.id),
        ])
        error_msgs = [m for m in bot_msgs if 'AI unavailable' in (m.body or '')]
        self.assertTrue(len(error_msgs) >= 1)

        # Verify error log exists
        error_logs = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
            ('status', '=', 'error'),
        ])
        self.assertTrue(len(error_logs) >= 1)

    # --- HTTPError: status kept, upstream body NOT stored in the log ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_error_body_not_stored(self, mock_post, mock_sleep):
        """HTTPError log keeps the status code but not the upstream response body."""
        self._add_visitor_msg()

        mock_response = MagicMock()

        mock_response.status_code = 200
        mock_response.status_code = 400
        mock_response.text = '{"error": {"message": "invalid_model"}}'
        http_error = requests.HTTPError('400 Bad Request', response=mock_response)
        mock_post.side_effect = http_error

        run_ai_job(self.env, self.session)

        error_logs = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
            ('status', '=', 'error'),
        ], order='id desc', limit=1)
        self.assertTrue(error_logs)
        self.assertIn('HTTP 400', error_logs[0].error_message or '')
        self.assertNotIn('invalid_model', error_logs[0].error_message or '')
        retry_logs = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
            ('status', '=', 'retry'),
        ])
        for log in retry_logs:
            self.assertNotIn('invalid_model', log.error_message or '')


class TestApiLogEdgeCases(TransactionCase):
    """Edge cases for _create_api_log and llm.api.log model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Log Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Log Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.channel.id,
        })

    def test_log_with_unicode_payload(self):
        """Unicode in request/response payload should be preserved (content logging on)."""
        self.channel.write({'ai_log_payload': True})
        self.channel._create_api_log(
            self.env,
            channel_id=self.channel.id,
            discuss_channel_id=self.session.id,
            model_name='gpt-4o',
            status='success',
            request_payload=[{'role': 'user', 'content': '你好世界'}],
            response_payload={'choices': [{'message': {'content': '回應'}}]},
        )
        log = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
        ], order='id desc', limit=1)
        self.assertIn('你好世界', log.request_payload)
        self.assertIn('回應', log.response_payload)

    def test_log_with_deleted_session(self):
        """Log creation should handle deleted discuss channel gracefully."""
        temp_session = self.env['discuss.channel'].create({
            'name': 'Temp Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(self.env).id,
            'livechat_channel_id': self.channel.id,
        })
        temp_id = temp_session.id
        temp_session.unlink()

        # Should not raise — FK is validated via .exists()
        self.channel._create_api_log(
            self.env,
            channel_id=self.channel.id,
            discuss_channel_id=temp_id,
            model_name='gpt-4o',
            status='success',
            request_payload=None,
            response_payload=None,
        )

        log = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
        ], order='id desc', limit=1)
        self.assertTrue(log.exists())
        # discuss_channel_id should not be set since session was deleted
        self.assertFalse(log.discuss_channel_id)

    def test_log_with_none_tokens(self):
        """None token values should default to 0."""
        self.channel._create_api_log(
            self.env,
            channel_id=self.channel.id,
            discuss_channel_id=self.session.id,
            model_name='gpt-4o',
            status='success',
            request_payload=None,
            response_payload=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
        )
        log = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
        ], order='id desc', limit=1)
        self.assertEqual(log.prompt_tokens, 0)
        self.assertEqual(log.completion_tokens, 0)
        self.assertEqual(log.total_tokens, 0)

    def test_log_large_error_message(self):
        """Very large error messages are cut to MAX_LOG_ERROR_LENGTH."""
        large_error = 'E' * 5000
        self.channel._create_api_log(
            self.env,
            channel_id=self.channel.id,
            discuss_channel_id=self.session.id,
            model_name='gpt-4o',
            status='error',
            request_payload=None,
            response_payload=None,
            error_message=large_error,
        )
        log = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
            ('status', '=', 'error'),
        ], order='id desc', limit=1)
        self.assertEqual(len(log.error_message), MAX_LOG_ERROR_LENGTH)

    def test_cleanup_boundary_13_days(self):
        """Log 13 days old should NOT be deleted (default retention 14 days)."""
        log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=13),
        })
        self.env['llm.api.log']._cleanup_old_logs()
        self.assertTrue(log.exists())

    def test_cleanup_15_days(self):
        """Log 15 days old should be deleted (default retention 14 days)."""
        log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=15),
        })
        self.env['llm.api.log']._cleanup_old_logs()
        self.assertFalse(log.exists())


class TestActionTestConnectionEdgeCases(TransactionCase):
    """Edge cases for action_test_ai_connection."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Connection Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
        })

    def test_missing_token_raises_error(self):
        channel = self.env['im_livechat.channel'].create({
            'name': 'No Token', 'ai_endpoint_url': 'https://litellm.test/v1', 'ai_model': 'gpt-4o',
        })
        with self.assertRaises(UserError):
            channel.action_test_ai_connection()

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_timeout_short_message(self, mock_post):
        mock_post.side_effect = requests.Timeout('timed out after 180s')
        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('逾時', result['params']['message'])
        self.assertNotIn('180s', result['params']['message'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_connection_error_short_message(self, mock_post):
        mock_post.side_effect = requests.ConnectionError('refused by 10.0.0.1')
        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('無法連線', result['params']['message'])
        self.assertNotIn('10.0.0.1', result['params']['message'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_error_shows_status_only(self, mock_post):
        """Failure shows the status code + short Chinese text, never the upstream body."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.status_code = 401
        mock_response.text = '{"error": "upstream-secret-detail"}'
        mock_response.raise_for_status.side_effect = requests.HTTPError(
            '401 Unauthorized', response=mock_response,
        )
        mock_post.return_value = mock_response
        result = self.channel.action_test_ai_connection()
        msg = result['params']['message']
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('HTTP 401', msg)
        self.assertIn('金鑰或簽章無效', msg)
        self.assertNotIn('upstream-secret-detail', msg)
        self.assertNotIn(TEST_API_KEY, msg)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_success_shows_status_not_reply(self, mock_post):
        """Success shows HTTP 200 and the model, not the model's reply text."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': '<think>reasoning</think>UNIQUE-REPLY'}}],
            'usage': {'total_tokens': 10},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = self.channel.action_test_ai_connection()
        msg = result['params']['message']
        self.assertEqual(result['params']['type'], 'success')
        self.assertIn('HTTP 200', msg)
        self.assertIn('gpt-4o', msg)
        self.assertNotIn('UNIQUE-REPLY', msg)
        self.assertNotIn('<think>', msg)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_invalid_response_short_message(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'error': 'no choices here'}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response
        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('回應格式不正確', result['params']['message'])
        self.assertNotIn('no choices here', result['params']['message'])


class TestBotPartnerEdgeCases(TransactionCase):
    """Edge cases for _get_or_create_bot_partner."""

    def test_empty_bot_name_defaults(self):
        """Empty bot name should default to 'AI 客服助理'."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'No Bot Name',
            'ai_bot_name': False,
        })
        partner = channel._get_or_create_bot_partner()
        self.assertEqual(partner.name, 'AI 客服助理')

    def test_unicode_bot_name(self):
        """Unicode bot name should be preserved."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'Unicode Bot',
            'ai_bot_name': 'AI助手',
        })
        partner = channel._get_or_create_bot_partner()
        self.assertEqual(partner.name, 'AI助手')

    def test_partner_deleted_recreated(self):
        """If bot partner is deleted, new one should be created."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'Deleted Partner Test',
            'ai_bot_name': 'TestBot',
        })
        partner1 = channel._get_or_create_bot_partner()
        pid1 = partner1.id

        # Simulate partner deletion (ondelete='set null')
        partner1.sudo().unlink()
        channel.invalidate_recordset()

        # ai_bot_partner_id should now be False
        partner2 = channel._get_or_create_bot_partner()
        self.assertTrue(partner2.exists())
        self.assertNotEqual(partner2.id, pid1)


class TestTriggerAiResponseEdgeCases(TransactionCase):
    """Edge cases for _trigger_ai_response."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Trigger Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Trigger Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.channel.id,
        })

    def _jobs(self):
        return self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', self.session.id)])

    def _msg(self):
        return self.env['mail.message'].create({
            'body': 'hi', 'model': 'discuss.channel', 'res_id': self.session.id,
            'message_type': 'comment', 'author_id': False,
        })

    @patch('odoo.addons.im_livechat_ai.models.livechat_ai_job.threading.Thread')
    def test_disabled_no_job(self, mock_thread):
        """Disabled AI should not queue a job."""
        self.channel.write({'ai_enabled': False})
        self.channel._trigger_ai_response(self.session, self._msg())
        self.assertFalse(self._jobs())
        mock_thread.assert_not_called()
        self.channel.write({'ai_enabled': True})

    @patch('odoo.addons.im_livechat_ai.models.livechat_ai_job.threading.Thread')
    def test_missing_token_no_job(self, mock_thread):
        """A channel whose token is gone (e.g. cleared in SQL) queues no job."""
        self.channel._ai_set_token(False)
        self.channel._trigger_ai_response(self.session, self._msg())
        self.assertFalse(self._jobs())
        mock_thread.assert_not_called()

    @patch('odoo.addons.im_livechat_ai.models.livechat_ai_job.threading.Thread')
    def test_job_queued_thread_only_after_commit(self, mock_thread):
        """The job is queued now; its (non-daemon) thread starts only after commit."""
        msg = self._msg()
        job = self.channel._trigger_ai_response(self.session, msg)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.message_id, msg)
        mock_thread.assert_not_called()
        callbacks = [f for f in self.env.cr.postcommit._funcs
                     if getattr(f, 'im_livechat_ai_job_ids', None) == job.ids]
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()  # what cr.commit() runs
        mock_thread.assert_called_once()
        self.assertIs(mock_thread.call_args[1].get('daemon'), False)
        self.assertEqual(mock_thread.call_args[1]['args'], (self.env.cr.dbname, job.id))


class TestIsVisitorMessageEdgeCases(TransactionCase):
    """Edge cases for _is_visitor_message — default bot partner fallback."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Visitor Edge Test',
            'ai_enabled': True,
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Visitor Edge Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.channel.id,
        })

    def _create_msg(self, author_id=None, author_guest_id=None):
        vals = {
            'body': 'test',
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'comment',
            # mail.message defaults the author to the current user's partner;
            # anonymous/guest visitors have none.
            'author_id': False,
        }
        if author_id:
            vals['author_id'] = author_id
        if author_guest_id:
            vals['author_guest_id'] = author_guest_id
        return self.env['mail.message'].create(vals)

    def test_default_bot_partner_excluded(self):
        """Default bot partner (from XML data) should be excluded."""
        default_bot = self.env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False)
        if default_bot:
            msg = self._create_msg(author_id=default_bot.id)
            self.assertFalse(self.session._is_visitor_message(msg))

    def test_guest_visitor_detected(self):
        """Guest visitor (author_guest_id set) should be detected."""
        guest = self.env['mail.guest'].create({'name': 'LINE User'})
        msg = self._create_msg(author_guest_id=guest.id)
        self.assertTrue(self.session._is_visitor_message(msg))

    def test_partner_with_user_not_visitor(self):
        """Partner linked to a user should NOT be detected as visitor."""
        partner = self.env['res.partner'].create({'name': 'Internal User Partner'})
        self.env['res.users'].create({
            'name': 'Internal User',
            'login': 'internal_edge_test',
            'partner_id': partner.id,
        })
        msg = self._create_msg(author_id=partner.id)
        self.assertFalse(self.session._is_visitor_message(msg))

    def test_partner_without_user_is_visitor(self):
        """Partner without any user should be detected as visitor."""
        partner = self.env['res.partner'].create({'name': 'External Contact'})
        msg = self._create_msg(author_id=partner.id)
        self.assertTrue(self.session._is_visitor_message(msg))
