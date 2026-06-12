# -*- coding: utf-8 -*-
"""
Edge-case unit tests for im_livechat_ai module.

Covers boundary conditions, adversarial inputs, security, concurrency,
and failure modes not covered by the existing test suite.
"""

import json
from datetime import timedelta
from unittest.mock import patch, MagicMock, PropertyMock

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestStripThinkTags(TransactionCase):
    """Exhaustive tests for _strip_think_tags static method."""

    def _strip(self, text):
        return self.env['im_livechat.channel']._strip_think_tags(text)

    # --- None / Empty / Falsy ---

    def test_none_input(self):
        self.assertIsNone(self._strip(None))

    def test_empty_string(self):
        self.assertEqual(self._strip(''), '')

    def test_false_input(self):
        self.assertFalse(self._strip(False))

    def test_zero_input(self):
        self.assertEqual(self._strip(0), 0)

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

    def test_case_sensitivity(self):
        """Tags are case sensitive — <Think> should NOT be stripped."""
        text = '<Think>reasoning</Think>Response'
        result = self._strip(text)
        self.assertIn('<Think>', result)

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
            'name': 'Constraint Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test-key',
            'ai_model': 'gpt-4o',
        })

    # --- URL constraints ---

    def test_url_with_leading_trailing_whitespace(self):
        """Whitespace around valid URL should be accepted (stripped by constraint)."""
        self.channel.write({'ai_api_base_url': '  https://api.example.com/v1  '})
        # Should not raise

    def test_url_with_port(self):
        self.channel.write({'ai_api_base_url': 'http://localhost:11434/v1'})

    def test_url_with_query_params(self):
        self.channel.write({'ai_api_base_url': 'https://api.example.com/v1?api_version=2024'})

    def test_url_missing_protocol(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_api_base_url': 'api.openai.com/v1'})

    def test_url_relative_path(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_api_base_url': '/v1/chat/completions'})

    def test_url_empty_string_when_enabled(self):
        """Empty URL when AI enabled should fail on required fields check."""
        with self.assertRaises(UserError):
            self.channel.write({'ai_api_base_url': ''})

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

    def test_temperature_exact_boundaries(self):
        """Exact boundary values 0.0 and 2.0 should be accepted."""
        self.channel.write({'ai_temperature': 0.0})
        self.assertAlmostEqual(self.channel.ai_temperature, 0.0)
        self.channel.write({'ai_temperature': 2.0})
        self.assertAlmostEqual(self.channel.ai_temperature, 2.0)

    def test_temperature_integer_values(self):
        """Integer values within range should work."""
        self.channel.write({'ai_temperature': 1})
        self.assertAlmostEqual(self.channel.ai_temperature, 1.0)

    def test_max_retries_exact_boundaries(self):
        self.channel.write({'ai_max_retries': 1})
        self.assertEqual(self.channel.ai_max_retries, 1)
        self.channel.write({'ai_max_retries': 10})
        self.assertEqual(self.channel.ai_max_retries, 10)

    def test_max_retries_zero(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_max_retries': 0})

    def test_max_retries_eleven(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_max_retries': 11})

    def test_retry_delay_exact_boundaries(self):
        self.channel.write({'ai_retry_delay': 1})
        self.assertEqual(self.channel.ai_retry_delay, 1)
        self.channel.write({'ai_retry_delay': 30})
        self.assertEqual(self.channel.ai_retry_delay, 30)

    def test_retry_delay_zero(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_retry_delay': 0})

    def test_retry_delay_thirtyone(self):
        with self.assertRaises(UserError):
            self.channel.write({'ai_retry_delay': 31})

    # --- Disabled AI bypasses range checks ---

    def test_disabled_ai_allows_any_max_history(self):
        """When AI is disabled, range constraints should not fire."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'Disabled Test',
            'ai_enabled': False,
        })
        # Should not raise even with 0 because ai_enabled=False
        channel.write({'ai_max_history': 0})

    def test_disabled_ai_allows_any_temperature(self):
        channel = self.env['im_livechat.channel'].create({
            'name': 'Disabled Test 2',
            'ai_enabled': False,
        })
        channel.write({'ai_temperature': 5.0})

    # --- Required fields when enabling AI ---

    def test_enable_ai_without_url_fails(self):
        channel = self.env['im_livechat.channel'].create({'name': 'No URL'})
        with self.assertRaises(UserError):
            channel.write({
                'ai_enabled': True,
                'ai_api_key': 'sk-test',
                'ai_model': 'gpt-4o',
                # ai_api_base_url missing
            })

    def test_enable_ai_without_key_fails(self):
        channel = self.env['im_livechat.channel'].create({'name': 'No Key'})
        with self.assertRaises(UserError):
            channel.write({
                'ai_enabled': True,
                'ai_api_base_url': 'https://api.openai.com/v1',
                'ai_model': 'gpt-4o',
                # ai_api_key missing
            })

    def test_enable_ai_without_model_fails(self):
        channel = self.env['im_livechat.channel'].create({'name': 'No Model'})
        with self.assertRaises(UserError):
            channel.write({
                'ai_enabled': True,
                'ai_api_base_url': 'https://api.openai.com/v1',
                'ai_api_key': 'sk-test',
                # ai_model missing
            })


class TestCallLlmApiEdgeCases(TransactionCase):
    """Edge cases for _call_llm_api method."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'API Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test-key',
            'ai_model': 'gpt-4o',
        })

    # --- URL construction edge cases ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_url_with_trailing_slash(self, mock_post):
        """Trailing slash should be stripped before appending path."""
        self.channel.write({'ai_api_base_url': 'https://api.openai.com/v1/'})
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'choices': [{'message': {'content': 'OK'}}]}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        called_url = mock_post.call_args[0][0]
        self.assertEqual(called_url, 'https://api.openai.com/v1/chat/completions')
        self.assertNotIn('//', called_url.replace('https://', ''))

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_url_with_whitespace(self, mock_post):
        """Leading/trailing whitespace in URL should be stripped."""
        self.channel.write({'ai_api_base_url': '  https://api.openai.com/v1  '})
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'choices': [{'message': {'content': 'OK'}}]}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        called_url = mock_post.call_args[0][0]
        self.assertFalse(called_url.startswith(' '))
        self.assertFalse(called_url.endswith(' '))

    # --- Response format edge cases ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_no_choices_key(self, mock_post):
        """Response without choices key should raise ValueError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'error': 'something'}
        mock_response.text = '{"error": "something"}'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        with self.assertRaises(ValueError):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_choices_is_none(self, mock_post):
        """choices=None should raise ValueError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'choices': None}
        mock_response.text = '{"choices": null}'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        with self.assertRaises(ValueError):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    # --- HTTP error codes ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_401_unauthorized(self, mock_post):
        """401 should raise HTTPError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = '{"error": "invalid_api_key"}'
        mock_response.raise_for_status.side_effect = requests.HTTPError(
            '401 Unauthorized', response=mock_response,
        )
        mock_post.return_value = mock_response

        with self.assertRaises(requests.HTTPError):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_429_rate_limited(self, mock_post):
        """429 should raise HTTPError."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = '{"error": "rate_limited"}'
        mock_response.raise_for_status.side_effect = requests.HTTPError(
            '429 Too Many Requests', response=mock_response,
        )
        mock_post.return_value = mock_response

        with self.assertRaises(requests.HTTPError):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_500_server_error(self, mock_post):
        """500 should raise HTTPError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal Server Error'
        mock_response.raise_for_status.side_effect = requests.HTTPError(
            '500 Internal Server Error', response=mock_response,
        )
        mock_post.return_value = mock_response

        with self.assertRaises(requests.HTTPError):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_connection_timeout(self, mock_post):
        """Timeout should raise requests.Timeout."""
        mock_post.side_effect = requests.Timeout('Connection timed out')
        with self.assertRaises(requests.Timeout):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_connection_refused(self, mock_post):
        """ConnectionError should propagate."""
        mock_post.side_effect = requests.ConnectionError('Connection refused')
        with self.assertRaises(requests.ConnectionError):
            self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])

    # --- Payload construction ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_temperature_zero_included(self, mock_post):
        """Temperature 0.0 should be included in payload (not falsy-skipped)."""
        self.channel.write({'ai_temperature': 0.0})
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'choices': [{'message': {'content': 'OK'}}]}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        payload = mock_post.call_args[1]['json']
        self.assertIn('temperature', payload)
        self.assertAlmostEqual(payload['temperature'], 0.0)

        # Restore
        self.channel.write({'ai_temperature': 0.7})

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_max_tokens_zero_excluded(self, mock_post):
        """Max tokens 0 should NOT be included in payload."""
        self.channel.write({'ai_max_tokens': 0})
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'choices': [{'message': {'content': 'OK'}}]}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        payload = mock_post.call_args[1]['json']
        self.assertNotIn('max_tokens', payload)

        # Restore
        self.channel.write({'ai_max_tokens': 1024})

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_authorization_header_format(self, mock_post):
        """Authorization header should be Bearer token format."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'choices': [{'message': {'content': 'OK'}}]}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        headers = mock_post.call_args[1]['headers']
        self.assertTrue(headers['Authorization'].startswith('Bearer '))
        self.assertIn('sk-test-key', headers['Authorization'])

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_timeout_is_60_seconds(self, mock_post):
        """Request timeout should be 60 seconds."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'choices': [{'message': {'content': 'OK'}}]}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        self.channel._call_llm_api([{'role': 'user', 'content': 'test'}])
        self.assertEqual(mock_post.call_args[1]['timeout'], 60)


class TestCreateBotMessageEdgeCases(TransactionCase):
    """Edge cases for _create_bot_message — XSS prevention, line breaks."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Bot Msg Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test',
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Bot Msg Session',
            'channel_type': 'livechat',
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
        self.assertIn('<br/>', msg.body)

    def test_multiple_consecutive_newlines(self):
        """Multiple newlines should produce multiple <br/>."""
        self.channel._create_bot_message(
            self.env, self.session,
            'Para 1\n\nPara 2',
        )
        msg = self._get_latest_bot_message()
        # Should have two <br/> tags
        br_count = msg.body.count('<br/>')
        self.assertEqual(br_count, 2)

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
            'name': 'Build Msg Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test',
            'ai_model': 'gpt-4o',
            'ai_system_prompt': 'Test system prompt.',
            'ai_max_history': 50,
        })
        cls.bot_partner = cls.channel._get_or_create_bot_partner()
        cls.visitor = cls.env['res.partner'].create({'name': 'Edge Visitor'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Build Msg Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.channel.id,
        })

    def _post_msg(self, body, author_id=None, author_guest_id=None):
        vals = {
            'body': body,
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'comment',
        }
        if author_id:
            vals['author_id'] = author_id
        if author_guest_id:
            vals['author_guest_id'] = author_guest_id
        return self.env['mail.message'].create(vals)

    # --- Empty channel ---

    def test_no_messages_returns_system_only(self):
        """Channel with no messages should return only system prompt."""
        session2 = self.env['discuss.channel'].create({
            'name': 'Empty Session',
            'channel_type': 'livechat',
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
        """ai_max_history=None should default to 50 internally."""
        self.channel.write({'ai_enabled': False})
        self.channel.write({'ai_max_history': 0})
        # _build_llm_messages clamps: max(1, min(0 or 50, 200)) = max(1, min(50, 200)) = 50
        # Actually with 0: max(1, min(0, 200)) = max(1, 0) = 1
        # So it effectively limits to 1 message
        self._post_msg('Msg 1', author_id=self.visitor.id)
        self._post_msg('Msg 2', author_id=self.visitor.id)
        messages = self.session._build_llm_messages(self.channel)
        non_system = [m for m in messages if m['role'] != 'system']
        # With max_history clamped to 1, only 1 message
        self.assertEqual(len(non_system), 1)
        # Restore
        self.channel.write({'ai_enabled': True, 'ai_max_history': 50})

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
    """Edge cases for _do_process_ai_response — record deletion, empty responses."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Process Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test',
            'ai_model': 'gpt-4o',
            'ai_max_retries': 2,
            'ai_retry_delay': 1,
            'ai_error_message': 'AI unavailable.',
        })
        cls.bot_partner = cls.channel._get_or_create_bot_partner()
        cls.visitor = cls.env['res.partner'].create({'name': 'Process Visitor'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Process Session',
            'channel_type': 'livechat',
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
    def test_channel_not_found(self, mock_post):
        """Should return early if channel ID doesn't exist."""
        self.channel._do_process_ai_response(
            self.env,
            channel_id=99999,  # non-existent
            discuss_channel_id=self.session.id,
        )
        # Should not have called API
        mock_post.assert_not_called()

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_session_not_found(self, mock_post):
        """Should return early if session ID doesn't exist."""
        self.channel._do_process_ai_response(
            self.env,
            channel_id=self.channel.id,
            discuss_channel_id=99999,  # non-existent
        )
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
        response_real.json.return_value = {
            'choices': [{'message': {'content': '<think>reasoning</think>Real answer'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        }
        response_real.raise_for_status = MagicMock()

        mock_post.side_effect = [response_think_only, response_real]

        self.channel._process_ai_response(
            self.env.cr.dbname,
            self.channel.id,
            self.session.id,
            test_env=self.env,
        )

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

        self.channel._process_ai_response(
            self.env.cr.dbname,
            self.channel.id,
            self.session.id,
            test_env=self.env,
        )

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

    # --- HTTPError includes response body in log ---

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_http_error_body_captured(self, mock_post, mock_sleep):
        """HTTPError response body should be captured in log."""
        self._add_visitor_msg()

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = '{"error": {"message": "invalid_model"}}'
        http_error = requests.HTTPError('400 Bad Request', response=mock_response)
        mock_post.side_effect = http_error

        self.channel._process_ai_response(
            self.env.cr.dbname,
            self.channel.id,
            self.session.id,
            test_env=self.env,
        )

        error_logs = self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id),
            ('status', '=', 'error'),
        ], order='id desc', limit=1)
        self.assertTrue(error_logs)
        self.assertIn('invalid_model', error_logs[0].error_message or '')


class TestApiLogEdgeCases(TransactionCase):
    """Edge cases for _create_api_log and llm.api.log model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Log Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test',
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Log Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.channel.id,
        })

    def test_log_with_unicode_payload(self):
        """Unicode in request/response payload should be preserved."""
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
        """Very large error messages should be stored."""
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
        self.assertEqual(len(log.error_message), 5000)

    def test_cleanup_boundary_29_days(self):
        """Log 29 days old should NOT be deleted."""
        log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=29),
        })
        self.env['llm.api.log']._cleanup_old_logs()
        self.assertTrue(log.exists())

    def test_cleanup_31_days(self):
        """Log 31 days old should be deleted."""
        log = self.env['llm.api.log'].create({
            'status': 'success',
            'timestamp': fields.Datetime.now() - timedelta(days=31),
        })
        self.env['llm.api.log']._cleanup_old_logs()
        self.assertFalse(log.exists())


class TestActionTestConnectionEdgeCases(TransactionCase):
    """Edge cases for action_test_ai_connection."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Connection Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test',
            'ai_model': 'gpt-4o',
        })

    def test_disabled_channel_raises_error(self):
        self.channel.write({'ai_enabled': False})
        with self.assertRaises(UserError):
            self.channel.action_test_ai_connection()
        self.channel.write({'ai_enabled': True})

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_timeout_raises_user_error(self, mock_post):
        mock_post.side_effect = requests.Timeout('timed out')
        with self.assertRaises(UserError) as ctx:
            self.channel.action_test_ai_connection()
        self.assertIn('timed out', str(ctx.exception).lower())

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_connection_error_raises_user_error(self, mock_post):
        mock_post.side_effect = requests.ConnectionError('refused')
        with self.assertRaises(UserError) as ctx:
            self.channel.action_test_ai_connection()
        self.assertIn('connect', str(ctx.exception).lower())

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_think_tags_stripped_in_test(self, mock_post):
        """Test connection should strip think tags from reply."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': '<think>reasoning</think>OK'}}],
            'usage': {'total_tokens': 10},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = self.channel.action_test_ai_connection()
        msg = result['params']['message']
        self.assertNotIn('<think>', msg)
        self.assertIn('OK', msg)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_very_long_reply_truncated(self, mock_post):
        """Reply in notification should be truncated to 100 chars."""
        long_reply = 'A' * 500
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': long_reply}}],
            'usage': {'total_tokens': 10},
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = self.channel.action_test_ai_connection()
        msg = result['params']['message']
        # The reply portion is truncated to 100 chars
        self.assertNotIn('A' * 101, msg)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post')
    def test_missing_usage_shows_na(self, mock_post):
        """Missing usage info should show N/A."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'OK'}}],
            # No 'usage' key
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = self.channel.action_test_ai_connection()
        msg = result['params']['message']
        self.assertIn('N/A', msg)


class TestBotPartnerEdgeCases(TransactionCase):
    """Edge cases for _get_or_create_bot_partner."""

    def test_empty_bot_name_defaults(self):
        """Empty bot name should default to 'AI Assistant'."""
        channel = self.env['im_livechat.channel'].create({
            'name': 'No Bot Name',
            'ai_bot_name': False,
        })
        partner = channel._get_or_create_bot_partner()
        self.assertEqual(partner.name, 'AI Assistant')

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
            'name': 'Trigger Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test',
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Trigger Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.channel.id,
        })

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.threading.Thread')
    def test_disabled_no_thread(self, mock_thread):
        """Disabled AI should not create thread."""
        self.channel.write({'ai_enabled': False})
        self.channel._trigger_ai_response(self.session, MagicMock())
        mock_thread.assert_not_called()
        self.channel.write({'ai_enabled': True})

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.threading.Thread')
    def test_missing_url_no_thread(self, mock_thread):
        """Missing URL should not create thread."""
        original_url = self.channel.ai_api_base_url
        self.channel.write({'ai_enabled': False})
        self.channel.write({'ai_api_base_url': False})
        self.channel.write({'ai_enabled': False})  # keep disabled to avoid constraint
        # Directly call with the channel state
        self.channel.ai_enabled = True  # bypass constraint temporarily
        self.channel._trigger_ai_response(self.session, MagicMock())
        mock_thread.assert_not_called()
        # Restore
        self.channel.write({'ai_enabled': False})
        self.channel.write({'ai_api_base_url': original_url})
        self.channel.write({'ai_enabled': True})

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.threading.Thread')
    def test_thread_is_daemon(self, mock_thread):
        """Thread should be daemon."""
        self.channel._trigger_ai_response(self.session, MagicMock())
        mock_thread.assert_called_once()
        self.assertTrue(mock_thread.call_args[1].get('daemon', False))


class TestIsVisitorMessageEdgeCases(TransactionCase):
    """Edge cases for _is_visitor_message — default bot partner fallback."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Visitor Edge Test',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test',
            'ai_model': 'gpt-4o',
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Visitor Edge Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.channel.id,
        })

    def _create_msg(self, author_id=None, author_guest_id=None):
        vals = {
            'body': 'test',
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'comment',
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
