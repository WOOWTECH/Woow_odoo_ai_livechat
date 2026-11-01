# -*- coding: utf-8 -*-
"""18.0.1.2.0: per-channel backend connection (OpenAI compatible / n8n).

Access to the connection fields, URL validation, the OpenAI request shape,
n8n signing and answers, the retry policy per backend, per-channel log
settings and the 18.0.1.1.0 -> 18.0.1.2.0 migration of the system
parameters. All HTTP is mocked; tokens are dummy values.
"""

import hashlib
import hmac
import importlib.util
import json
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger

from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    LLM_MAX_RETRIES,
    LLM_REQUEST_TIMEOUT,
    LLM_RETRY_BACKOFF,
    MAX_N8N_REPLIES,
    MAX_REQUEST_TIMEOUT,
    N8N_REQUEST_TIMEOUT,
    N8N_TEST_TIMEOUT,
    TOKEN_COLUMN,
    endpoint_url_allowed,
    n8n_signature_headers,
)

from .common import (
    TEST_API_KEY,
    TEST_BACKEND,
    TEST_BASE_URL,
    TEST_N8N_BACKEND,
    TEST_N8N_SECRET,
    TEST_N8N_URL,
    call_backend,
    ok_response,
)

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
SLEEP = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep'
CHANNEL_LOGGER = 'odoo.addons.im_livechat_ai.models.im_livechat_channel'


def _json_response(data, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = data
    response.text = json.dumps(data)
    return response


def _not_sent_error():
    """What requests raises when the connection could not be opened."""
    return requests.ConnectionError(MaxRetryError(None, '/webhook', NewConnectionError(None, 'refused')))


class BackendCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(
            cls.env, login='be_manager', name='Live Chat 主管',
            groups='base.group_user,im_livechat.im_livechat_group_manager',
        )
        cls.admin = new_test_user(
            cls.env, login='be_admin', name='系統管理員',
            groups='base.group_user,base.group_system,im_livechat.im_livechat_group_manager',
        )
        cls.operator_user = new_test_user(
            cls.env, login='be_operator', name='客服小明',
            groups='base.group_user,im_livechat.im_livechat_group_user',
        )
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': '官網客服',
            'ai_enabled': True,
            'ai_system_prompt': '你是官網客服。',
            'ai_max_retries': 0,
        })
        cls.n8n = cls.env['im_livechat.channel'].create({
            **TEST_N8N_BACKEND,
            'name': 'n8n 客服',
            'ai_enabled': True,
            'ai_max_retries': 2,
        })
        cls.env['ir.config_parameter'].sudo().set_param('database.uuid', '0edfec80-1111-2222-3333-444455556666')

    def _guest_session(self, channel, text='請問營業時間？', guest_name='訪客 42'):
        guest = self.env['mail.guest'].create({'name': guest_name, 'lang': 'en_US'})
        session = self.env['discuss.channel'].create({
            'name': 'Guest Session',
            'channel_type': 'livechat',
            'livechat_operator_id': self.operator_user.partner_id.id,
            'livechat_channel_id': channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator_user.partner_id.id})],
        })
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'guest_id': guest.id})
        message = self.env['mail.message'].create({
            'body': text, 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': False, 'author_guest_id': guest.id,
        }) if text else None
        return session, guest, message

    def _post_bot(self, session, channel, text):
        return self.env['mail.message'].create({
            'body': text, 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': channel.ai_bot_partner_id.id,
        })


class TestConnectionAccess(BackendCase):
    """Owner decision 4: the connection is per channel and only system
    administrators read or write the URL and the token."""

    def test_manager_cannot_read_url_or_token(self):
        Channel = self.env['im_livechat.channel'].with_user(self.manager)
        for fname in ('ai_endpoint_url', 'ai_api_token', 'ai_api_token_set', 'ai_log_payload'):
            with self.assertRaises(AccessError, msg=fname):
                Channel.browse(self.channel.id).read([fname])
        values = Channel.browse(self.channel.id).read(['ai_connection_ready', 'ai_backend_type', 'ai_model'])[0]
        self.assertTrue(values['ai_connection_ready'])
        self.assertEqual(values['ai_backend_type'], 'openai')
        # A read without field list leaves the restricted fields out.
        row = Channel.search_read([('id', '=', self.channel.id)])[0]
        for fname in ('ai_endpoint_url', 'ai_api_token', 'ai_api_token_set', 'ai_log_payload'):
            self.assertNotIn(fname, row)
        self.assertNotIn(TEST_API_KEY, json.dumps(row, default=str))

    def test_manager_cannot_write_connection(self):
        channel = self.channel.with_user(self.manager)
        for vals in ({'ai_endpoint_url': 'https://evil.example/v1'}, {'ai_api_token': 'dummy-evil'},
                     {'ai_backend_type': 'n8n'}, {'ai_log_payload': True}):
            with self.assertRaises(AccessError, msg=vals), self.env.cr.savepoint():
                channel.write(vals)
        with self.assertRaises(AccessError):
            self.env['im_livechat.channel'].with_user(self.manager).create({
                'name': 'Manager URL', 'ai_endpoint_url': 'https://evil.example/v1',
            })
        # The unchanged backend type (sent by the form) is accepted.
        channel.write({'ai_backend_type': 'openai', 'ai_model': 'other/model', 'ai_system_prompt': '新提示'})
        self.assertEqual(self.channel.ai_endpoint_url, TEST_BASE_URL)
        self.assertEqual(self.channel._ai_get_token(), TEST_API_KEY)
        # A manager may create a channel (no connection yet) and switch AI off/on.
        new = self.env['im_livechat.channel'].with_user(self.manager).create({
            'name': 'Manager Channel', 'ai_backend_type': 'openai', 'ai_model': 'x',
        })
        self.assertFalse(new.sudo().ai_connection_ready)
        channel.write({'ai_enabled': False})
        channel.write({'ai_enabled': True})

    def test_manager_cannot_enable_without_connection(self):
        new = self.env['im_livechat.channel'].create({'name': 'No Connection', 'ai_model': 'x'})
        with self.assertRaises(UserError) as caught:
            new.with_user(self.manager).write({'ai_enabled': True})
        self.assertIn('尚未設定後端連線', str(caught.exception))
        self.assertIn('系統管理員', str(caught.exception))

    def test_admin_reads_and_writes(self):
        channel = self.channel.with_user(self.admin)
        channel.write({'ai_endpoint_url': 'https://gateway.example/v1', 'ai_api_token': 'dummy-new-token'})
        values = channel.read(['ai_endpoint_url', 'ai_api_token', 'ai_api_token_set', 'ai_connection_ready'])[0]
        self.assertEqual(values['ai_endpoint_url'], 'https://gateway.example/v1')
        # Write-only: the token is never read back, only "saved".
        self.assertFalse(values['ai_api_token'])
        self.assertTrue(values['ai_api_token_set'])
        self.assertTrue(values['ai_connection_ready'])
        self.assertEqual(self.channel._ai_get_token(), 'dummy-new-token')
        # Empty input keeps the saved token.
        channel.write({'ai_api_token': ''})
        channel.write({'ai_api_token': False, 'ai_model': 'm2'})
        self.assertEqual(self.channel._ai_get_token(), 'dummy-new-token')

    def test_token_is_not_a_stored_field(self):
        self.assertFalse(self.channel._fields['ai_api_token'].store)
        self.assertNotIn(TOKEN_COLUMN, self.channel._fields)
        self.env.flush_all()
        self.env.cr.execute("SELECT count(*) FROM im_livechat_channel WHERE ai_api_token_secret IS NOT NULL")
        self.assertGreaterEqual(self.env.cr.fetchone()[0], 2)

    def test_web_save_does_not_echo_token(self):
        result = self.channel.with_user(self.admin).web_save(
            {'ai_api_token': 'dummy-typed-token'}, {'ai_api_token': {}, 'ai_api_token_set': {}})
        self.assertFalse(result[0]['ai_api_token'])
        self.assertTrue(result[0]['ai_api_token_set'])

    def test_form_fields_per_group(self):
        def arch(user):
            return self.env['im_livechat.channel'].with_user(user).get_views(
                [(False, 'form')])['views']['form']['arch']
        manager_arch = arch(self.manager)
        for fname in ('ai_endpoint_url', 'ai_api_token', 'ai_log_payload', 'action_test_ai_connection'):
            self.assertNotIn(fname, manager_arch)
        for fname in ('ai_connection_ready', 'ai_backend_type', 'ai_model', 'action_view_api_logs',
                      'ai_log_retention_days', 'ai_job_warning'):
            self.assertIn(fname, manager_arch)
        admin_arch = arch(self.admin)
        for fname in ('ai_endpoint_url', 'ai_api_token', 'ai_log_payload', 'action_test_ai_connection'):
            self.assertIn(fname, admin_arch)

    @patch(POST)
    def test_connection_test_is_admin_only(self, mock_post):
        mock_post.return_value = ok_response()
        with self.assertRaises(AccessError):
            self.channel.with_user(self.manager).action_test_ai_connection()
        mock_post.assert_not_called()
        result = self.channel.with_user(self.admin).action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')
        payload = mock_post.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'chatgpt-pool/gpt-6-sol')
        self.assertNotIn('max_tokens', payload)
        self.assertNotIn(TEST_API_KEY, result['params']['message'])

    def test_copy_has_no_token(self):
        copy = self.channel.with_user(self.manager).copy()
        self.assertFalse(copy.sudo().ai_enabled)
        self.assertFalse(copy.sudo().ai_api_token_set)


class TestEndpointUrlValidation(BackendCase):
    """https only; http only for local and in-cluster hosts (woow_ai_contact rule)."""

    def test_allowed_and_refused(self):
        allowed = (
            'https://litellm.example.com/v1', 'https://n8n.example.com/webhook/x',
            'http://localhost:4000/v1', 'http://127.0.0.1:8642/v1', 'http://[::1]:8642/v1',
            'http://hermes.ai.svc.cluster.local:8642/v1', 'HTTPS://Upper.Example/v1',
        )
        refused = (
            'http://hermes:8642/v1', 'http://litellm.example.com/v1', 'ftp://example.com',
            'litellm.example.com/v1', 'javascript:alert(1)', 'http://svc.cluster.local.evil.com/v1',
            'https://', 'not a url',
        )
        for url in allowed:
            self.assertTrue(endpoint_url_allowed(url), url)
        for url in refused:
            self.assertFalse(endpoint_url_allowed(url), url)

    def test_constraint_on_save(self):
        for url in ('http://hermes:8642/v1', 'http://example.com/hook', 'ftp://x.example'):
            with self.assertRaises(ValidationError, msg=url), self.env.cr.savepoint():
                self.channel.write({'ai_endpoint_url': url})
        self.channel.write({'ai_endpoint_url': 'http://hermes.ai.svc.cluster.local:8642/v1'})
        self.assertTrue(self.channel.ai_connection_ready)

    @patch(POST)
    def test_url_set_in_sql_is_refused_at_runtime(self, mock_post):
        """A URL that bypassed the constraint (e.g. migrated) is never called."""
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_endpoint_url = 'http://litellm:4000/v1' "
                            "WHERE id = %s", (self.channel.id,))
        self.env.invalidate_all()
        self.assertFalse(self.channel.ai_connection_ready)
        with mute_logger(CHANNEL_LOGGER):
            result = call_backend(self.channel)
        mock_post.assert_not_called()
        self.assertIn('尚未設定 AI 後端連線', result['error'])


class TestOpenAiRequest(BackendCase):
    """openai: POST {url}/chat/completions, no tools, no temperature."""

    @patch(POST)
    def test_request_shape(self, mock_post):
        mock_post.return_value = ok_response('您好')
        session, guest, _message = self._guest_session(self.channel)
        result = call_backend(self.channel, session=session)
        self.assertEqual(result['replies'], ['您好'])
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], TEST_BASE_URL + '/chat/completions')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + TEST_API_KEY)
        self.assertEqual(kwargs['timeout'], LLM_REQUEST_TIMEOUT)
        self.assertIs(kwargs['allow_redirects'], False)
        payload = kwargs['json']
        self.assertEqual(list(payload), ['model', 'messages', 'user', 'metadata'])
        session_id = 'woow-ai-0edfec80-%s' % session.id
        self.assertEqual(payload['user'], session_id)
        self.assertEqual(payload['messages'], [
            {'role': 'system', 'content': '你是官網客服。'},
            {'role': 'user', 'content': '請問營業時間？'},
        ])
        metadata = payload['metadata']
        self.assertEqual(metadata['session_id'], session_id)
        self.assertEqual(metadata['odoo_guest_id'], str(guest.id))
        self.assertEqual(metadata['odoo_is_guest'], 'true')
        self.assertEqual(metadata['odoo_is_portal'], 'false')
        self.assertEqual(metadata['odoo_name'], '訪客 42')
        self.assertEqual(metadata['livechat_channel'], '官網客服')
        self.assertTrue(all(isinstance(v, str) for v in metadata.values()))
        self.assertTrue(set(metadata) <= {'session_id', 'odoo_partner_id', 'odoo_guest_id', 'odoo_name',
                                          'odoo_lang', 'odoo_is_portal', 'odoo_is_guest', 'livechat_channel'})

    @patch(POST)
    def test_portal_identity_has_no_email_in_metadata(self, mock_post):
        mock_post.return_value = ok_response()
        portal = new_test_user(self.env, login='be_portal', name='王小明', email='portal@example.com',
                               groups='base.group_portal')
        session = self.env['discuss.channel'].create({
            'name': 'Portal Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.operator_user.partner_id.id,
            'livechat_channel_id': self.channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator_user.partner_id.id}),
                                   (0, 0, {'partner_id': portal.partner_id.id})],
        })
        self.env['mail.message'].create({
            'body': '我的訂單呢？', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': portal.partner_id.id,
        })
        call_backend(self.channel, session=session)
        payload = mock_post.call_args.kwargs['json']
        self.assertEqual(payload['metadata']['odoo_partner_id'], str(portal.partner_id.id))
        self.assertEqual(payload['metadata']['odoo_is_portal'], 'true')
        self.assertEqual(payload['metadata']['odoo_is_guest'], 'false')
        self.assertNotIn('portal@example.com', json.dumps(payload, ensure_ascii=False))

    @patch(POST)
    def test_messages_start_and_end_with_user_turn(self, mock_post):
        """The welcome message before the first visitor message is dropped; a
        bot message after the last visitor message moves before it."""
        mock_post.return_value = ok_response()
        self.channel.write({'ai_system_prompt': False})
        session, guest, _m = self._guest_session(self.channel, text=None)
        self._post_bot(session, self.channel, '歡迎光臨')
        self.env['mail.message'].create({
            'body': '有現貨嗎？', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': False, 'author_guest_id': guest.id,
        })
        self._post_bot(session, self.channel, '抱歉，AI 暫時無法回覆。')
        call_backend(self.channel, session=session)
        messages = mock_post.call_args.kwargs['json']['messages']
        # No prompt and no event: no system message at all.
        self.assertEqual(messages, [
            {'role': 'user', 'content': '[Messages you (the assistant) sent before this user message]\n'
                                        '抱歉，AI 暫時無法回覆。'},
            {'role': 'user', 'content': '有現貨嗎？'},
        ])
        self.assertEqual(messages[0]['role'], 'user')
        self.assertEqual(messages[-1], {'role': 'user', 'content': '有現貨嗎？'})

    @patch(SLEEP)
    @patch(POST)
    def test_openai_keeps_retry_policy(self, mock_post, mock_sleep):
        """openai: every failure is retried within ai_max_retries (3 s, 10 s)."""
        self.channel.write({'ai_max_retries': 2})
        bad = ok_response()
        bad.status_code = 502
        mock_post.side_effect = [bad, requests.ReadTimeout('slow'), ok_response('第三次')]
        with mute_logger(CHANNEL_LOGGER):
            result = call_backend(self.channel)
        self.assertEqual(result['replies'], ['第三次'])
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [3, 10])


class TestN8nRequest(BackendCase):
    """n8n: woow_ai_contact docs/PROTOCOL.md "n8n backend"."""

    def _call(self, mock_post, response, session=None):
        mock_post.return_value = response
        if session is None:
            session, _guest, _m = self._guest_session(self.n8n)
        return call_backend(self.n8n, session=session), session

    @patch(POST)
    def test_signed_request(self, mock_post):
        before = int(time.time())
        result, session = self._call(mock_post, _json_response({'reply': {'text': '您好'}}))
        self.assertEqual(result['replies'], ['您好'])
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], TEST_N8N_URL)
        headers = kwargs['headers']
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('json', kwargs)
        body = kwargs['data']
        self.assertIsInstance(body, bytes)
        ts = headers['X-Woow-Timestamp']
        self.assertTrue(ts.isdigit())
        self.assertLessEqual(abs(int(ts) - before), 5)
        expected = 'sha256=' + hmac.new(TEST_N8N_SECRET.encode(), ts.encode() + b'.' + body,
                                        hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Woow-Signature'], expected)
        self.assertNotIn(TEST_N8N_SECRET.encode(), body)
        self.assertNotIn(TEST_N8N_SECRET, json.dumps(headers))
        # Compact UTF-8 JSON (the exact bytes that were signed)
        self.assertIn('營業時間'.encode(), body)
        self.assertNotIn(b'": ', body)

    def test_signature_helper(self):
        headers = n8n_signature_headers('whsec-dummy', b'{"a":1}', timestamp=1793430000)
        self.assertEqual(headers['X-Woow-Timestamp'], '1793430000')
        self.assertEqual(headers['X-Woow-Signature'], 'sha256=' + hmac.new(
            b'whsec-dummy', b'1793430000.{"a":1}', hashlib.sha256).hexdigest())

    @patch(POST)
    def test_payload(self, mock_post):
        self.n8n.write({'ai_system_prompt': '你是 n8n 客服。'})
        session, guest, message = self._guest_session(self.n8n, text='第一個問題')
        self._post_bot(session, self.n8n, '第一個回答')
        second = self.env['mail.message'].create({
            'body': '<p>第二個問題</p>', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': False, 'author_guest_id': guest.id,
        })
        self._call(mock_post, _json_response({'skip': True}), session=session)
        payload = json.loads(mock_post.call_args.kwargs['data'])
        self.assertEqual(payload['event'], 'message')
        self.assertEqual(payload['session_id'], 'woow-ai-0edfec80-%s' % session.id)
        self.assertEqual(payload['contact'], {'id': self.n8n.id, 'name': 'n8n 客服'})
        self.assertEqual(payload['system_prompt'], '你是 n8n 客服。')
        self.assertEqual(payload['message'], {'id': second.id, 'text': '第二個問題', 'attachments': []})
        self.assertEqual(payload['history'], [
            {'role': 'user', 'text': '第一個問題', 'attachments': []},
            {'role': 'assistant', 'text': '第一個回答', 'attachments': []},
        ])
        self.assertEqual(payload['events'], [])
        # n8n Chat Trigger aliases next to the protocol fields
        self.assertEqual(payload['action'], 'sendMessage')
        self.assertEqual(payload['sessionId'], payload['session_id'])
        self.assertEqual(payload['chatInput'], '第二個問題')
        user = payload['user']
        self.assertEqual(user['guest_id'], guest.id)
        self.assertIsNone(user['user_id'])
        self.assertIsNone(user['partner_id'])
        self.assertTrue(user['is_guest'])
        self.assertFalse(user['is_portal'])
        self.assertEqual(user['visitor_key'], 'guest:%s' % guest.id)
        self.assertEqual(user['email'], '')
        channel = payload['channel']
        self.assertEqual(channel['id'], session.id)
        self.assertEqual(channel['type'], 'livechat')
        livechat = channel['livechat']
        self.assertEqual(livechat['channel_id'], self.n8n.id)
        self.assertEqual(livechat['channel_name'], 'n8n 客服')
        self.assertEqual(livechat['livechat_channel_id'], self.n8n.id)
        self.assertEqual(livechat['livechat_channel_name'], 'n8n 客服')
        self.assertEqual(livechat['visitor_kind'], 'guest')
        self.assertEqual(livechat['operator_name'], '客服小明')

    @patch(POST)
    def test_events_for_operator_and_resume(self, mock_post):
        session, guest, _m = self._guest_session(self.n8n)
        self.env['mail.message'].create({
            'body': '我來幫您', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': self.operator_user.partner_id.id,
        })
        self.env['mail.message'].create({
            'body': '謝謝', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': False, 'author_guest_id': guest.id,
        })
        mock_post.return_value = _json_response({'skip': True})
        conversation = session._ai_conversation(self.n8n, resume=True)
        self.n8n._generate_ai_reply(session, conversation)
        payload = json.loads(mock_post.call_args.kwargs['data'])
        self.assertEqual([e['kind'] for e in payload['events']], ['operator', 'resume'])
        for event in payload['events']:
            self.assertTrue(event['time'].endswith('Z'))
            self.assertTrue(event['text'])
        self.assertIn({'role': 'user', 'text': '[真人客服 客服小明] 我來幫您', 'attachments': []},
                      payload['history'])
        self.assertEqual(payload['message']['text'], '謝謝')
        # The channel prompt stays the channel prompt (events are separate).
        self.assertEqual(payload['system_prompt'], '')

    @patch(POST)
    def test_answer_forms(self, mock_post):
        cases = (
            ({'reply': {'text': '一則'}}, ['一則'], False),
            ({'reply': '純文字'}, ['純文字'], False),
            ({'replies': [{'text': '第一則'}, '第二則']}, ['第一則', '第二則'], False),
            ([{'reply': {'text': '項目清單'}}], ['項目清單'], False),
            ({'reply': {'text': '<think>想一下</think>答案'}}, ['答案'], False),
            ({'skip': True}, [], True),
            ({'replies': [{'text': str(i)} for i in range(15)]}, [str(i) for i in range(MAX_N8N_REPLIES)], False),
            # n8n AI Agent / Chat Trigger default answer
            ({'output': '**粗體**與[連結](https://aiot.example/shop/a)'}, ['**粗體**與[連結](https://aiot.example/shop/a)'], False),
            ([{'output': '項目清單的 output'}], ['項目清單的 output'], False),
            ({'output': '<think>x</think>答案'}, ['答案'], False),
        )
        for data, replies, skip in cases:
            with self.subTest(data=data):
                result, _s = self._call(mock_post, _json_response(data))
                self.assertFalse(result['error'])
                self.assertEqual(result['replies'], replies)
                self.assertIs(result['skip'], skip)

    @patch(POST)
    def test_invalid_answers_are_errors(self, mock_post):
        cases = (
            ({'text': 'no reply key'}, '回應格式不正確'),
            ({'reply': {'text': ''}}, 'AI 回覆是空的'),
            ({'reply': {'text': '   '}}, 'AI 回覆是空的'),
            ({'replies': 'not a list'}, '回應格式不正確'),
            ({'replies': [123]}, '回應格式不正確'),
            ({'skip': 'yes'}, '回應格式不正確'),
            ('just a string', '回應格式不正確'),
            ({'output': ''}, 'AI 回覆是空的'),
            ({'output': {'text': 'x'}}, '回應格式不正確'),
            ([{'output': None}], '回應格式不正確'),
        )
        with mute_logger(CHANNEL_LOGGER):
            for data, error in cases:
                with self.subTest(data=data):
                    result, _s = self._call(mock_post, _json_response(data))
                    self.assertFalse(result['replies'])
                    self.assertIn(error, result['error'])
            not_json = _json_response({})
            not_json.json.side_effect = ValueError('no json')
            result, _s = self._call(mock_post, not_json)
            self.assertIn('回應格式不正確', result['error'])
            result, _s = self._call(mock_post, _json_response({'error': 'agent_failed'}, status=502))
            self.assertIn('HTTP 502', result['error'])
        # Each of these was one call: answers are never retried.
        self.assertEqual(mock_post.call_count, len(cases) + 2)

    @patch(SLEEP)
    @patch(POST)
    def test_retry_policy(self, mock_post, mock_sleep):
        """n8n is retried only when the call could not be delivered or got
        HTTP 429 / 503 (decision 26 of woow_ai_contact)."""
        never = (
            _json_response({}, status=500), _json_response({}, status=502), _json_response({}, status=504),
            _json_response({}, status=524), _json_response({}, status=401),
            requests.ReadTimeout('read timeout'),
            requests.ConnectionError('Connection aborted after the request was sent'),
        )
        with mute_logger(CHANNEL_LOGGER):
            for outcome in never:
                with self.subTest(outcome=outcome):
                    mock_post.reset_mock(side_effect=True, return_value=True)
                    mock_sleep.reset_mock()
                    mock_post.side_effect = [outcome]
                    result, _s = self._call(mock_post, None)
                    self.assertTrue(result['error'])
                    self.assertEqual(mock_post.call_count, 1)
                    mock_sleep.assert_not_called()
            retried = (_json_response({}, status=429), _json_response({}, status=503),
                       _not_sent_error(), requests.ConnectTimeout('connect timeout'))
            for outcome in retried:
                with self.subTest(outcome=outcome):
                    mock_post.reset_mock(side_effect=True, return_value=True)
                    mock_sleep.reset_mock()
                    mock_post.side_effect = [outcome, _json_response({'reply': '好了'})]
                    result, _s = self._call(mock_post, None)
                    self.assertEqual(result['replies'], ['好了'])
                    self.assertEqual(mock_post.call_count, 2)
                    self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [3])
            # At most ai_max_retries (2) retries.
            mock_post.reset_mock(side_effect=True, return_value=True)
            mock_sleep.reset_mock()
            mock_post.side_effect = [_json_response({}, status=503)] * 5
            result, _s = self._call(mock_post, None)
            self.assertIn('HTTP 503', result['error'])
            self.assertEqual(mock_post.call_count, 3)
            self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [3, 10])

    @patch(POST)
    def test_retry_is_signed_again(self, mock_post):
        with patch(SLEEP):
            mock_post.side_effect = [_json_response({}, status=429), _json_response({'reply': 'ok'})]
            self._call(mock_post, None)
        for call in mock_post.call_args_list:
            headers, body = call.kwargs['headers'], call.kwargs['data']
            expected = 'sha256=' + hmac.new(TEST_N8N_SECRET.encode(),
                                            headers['X-Woow-Timestamp'].encode() + b'.' + body,
                                            hashlib.sha256).hexdigest()
            self.assertEqual(headers['X-Woow-Signature'], expected)

    @patch(POST)
    def test_connection_test_ping(self, mock_post):
        mock_post.return_value = _json_response({'skip': True})
        result = self.n8n.with_user(self.admin).action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')
        kwargs = mock_post.call_args.kwargs
        body = kwargs['data']
        self.assertEqual(json.loads(body), {
            'event': 'ping', 'contact': {'id': self.n8n.id, 'name': 'n8n 客服'},
            'action': 'loadPreviousSession', 'sessionId': 'woow-ai-ping-%s' % self.n8n.id,
        })
        self.assertNotIn('Authorization', kwargs['headers'])
        expected = 'sha256=' + hmac.new(TEST_N8N_SECRET.encode(),
                                        kwargs['headers']['X-Woow-Timestamp'].encode() + b'.' + body,
                                        hashlib.sha256).hexdigest()
        self.assertEqual(kwargs['headers']['X-Woow-Signature'], expected)
        mock_post.return_value = _json_response({'error': 'bad signature'}, status=401)
        with mute_logger(CHANNEL_LOGGER):
            result = self.n8n.with_user(self.admin).action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('HTTP 401', result['params']['message'])
        self.assertNotIn('bad signature', result['params']['message'])


class TestN8nChatTrigger(BackendCase):
    """n8n Chat Trigger ("hosted chat") workflows: public, no signing secret,
    answer {"output": ...}, slow agents (owner addendum to the 1.2.0 spec)."""

    CHAT_URL = 'https://n8n.test/webhook/0b1c2d3e-advisor/chat'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.chat = cls.env['im_livechat.channel'].create({
            'name': '智居顧問', 'ai_backend_type': 'n8n', 'ai_endpoint_url': cls.CHAT_URL,
            'ai_enabled': True, 'ai_max_retries': 2,
        })

    def test_token_optional_for_n8n_only(self):
        self.assertTrue(self.chat.ai_enabled)
        self.assertTrue(self.chat.ai_connection_ready)
        self.assertFalse(self.chat.ai_api_token_set)
        openai = self.env['im_livechat.channel'].create({
            'name': 'OpenAI no token', 'ai_endpoint_url': TEST_BASE_URL, 'ai_model': 'm',
        })
        self.assertFalse(openai.ai_connection_ready)
        with self.assertRaises(UserError):
            openai.write({'ai_enabled': True})

    @patch(POST)
    def test_unsigned_request_and_output_answer(self, mock_post):
        mock_post.return_value = _json_response({'output': '推薦 **A1**：[A1](https://aiot.example/shop/a1)'})
        session, _guest, message = self._guest_session(self.chat, text='臥室燈怎麼選？')
        result = call_backend(self.chat, session=session)
        self.assertEqual(result['replies'], ['推薦 **A1**：[A1](https://aiot.example/shop/a1)'])
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], self.CHAT_URL)
        self.assertNotIn('X-Woow-Signature', kwargs['headers'])
        self.assertNotIn('X-Woow-Timestamp', kwargs['headers'])
        self.assertNotIn('Authorization', kwargs['headers'])
        body = json.loads(kwargs['data'])
        self.assertEqual(body['action'], 'sendMessage')
        self.assertEqual(body['chatInput'], '臥室燈怎麼選？')
        self.assertEqual(body['sessionId'], 'woow-ai-0edfec80-%s' % session.id)
        self.assertEqual(body['sessionId'], body['session_id'])
        # The protocol fields are still there for Webhook-node workflows.
        self.assertEqual(body['event'], 'message')
        self.assertEqual(body['message']['text'], '臥室燈怎麼選？')

    @patch(POST)
    def test_reply_job_posts_output(self, mock_post):
        mock_post.return_value = _json_response([{'output': '您好，**歡迎**'}])
        session, _guest, message = self._guest_session(self.chat)
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertIn('<strong>歡迎</strong>', job.reply_message_id.body)

    @patch(POST)
    def test_timeouts_per_backend(self, mock_post):
        self.assertEqual(N8N_REQUEST_TIMEOUT, 120)
        self.assertEqual(LLM_REQUEST_TIMEOUT, 45)
        mock_post.return_value = _json_response({'output': 'ok'})
        call_backend(self.chat)
        self.assertEqual(mock_post.call_args.kwargs['timeout'], 120)
        mock_post.return_value = ok_response()
        call_backend(self.channel)
        self.assertEqual(mock_post.call_args.kwargs['timeout'], 45)

    @patch(SLEEP)
    @patch(POST)
    def test_read_timeout_not_retried(self, mock_post, mock_sleep):
        """The agent may still be running: a retry would double the cost."""
        mock_post.side_effect = [requests.ReadTimeout('no answer in 90 s'), _json_response({'output': 'late'})]
        with mute_logger(CHANNEL_LOGGER):
            result = call_backend(self.chat)
        self.assertIn('連線逾時', result['error'])
        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @patch(POST)
    def test_connection_test_without_secret(self, mock_post):
        mock_post.return_value = _json_response({'data': []})
        result = self.chat.with_user(self.admin).action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')
        kwargs = mock_post.call_args.kwargs
        self.assertNotIn('X-Woow-Signature', kwargs['headers'])
        body = json.loads(kwargs['data'])
        # A Chat Trigger answers loadPreviousSession without running the agent.
        self.assertEqual(body['action'], 'loadPreviousSession')
        self.assertEqual(body['event'], 'ping')
        self.assertNotIn('chatInput', body)

    @patch(POST)
    def test_secret_set_later_signs(self, mock_post):
        self.chat.write({'ai_api_token': 'dummy-chat-secret'})
        mock_post.return_value = _json_response({'output': 'ok'})
        call_backend(self.chat)
        headers, body = mock_post.call_args.kwargs['headers'], mock_post.call_args.kwargs['data']
        expected = 'sha256=' + hmac.new(b'dummy-chat-secret', headers['X-Woow-Timestamp'].encode() + b'.' + body,
                                        hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Woow-Signature'], expected)


class TestN8nJobs(BackendCase):
    """Replies, skip and errors through the reply job."""

    def _bot_messages(self, session):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', session.id),
            ('author_id', '=', self.n8n.ai_bot_partner_id.id),
        ], order='id')

    def _run(self, session, message):
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        job._run_job(commit=False)
        return job

    @patch(POST)
    def test_replies_are_posted(self, mock_post):
        mock_post.return_value = _json_response({'replies': [{'text': '第一則'}, {'text': '**第二則**'}]})
        session, _guest, message = self._guest_session(self.n8n)
        job = self._run(session, message)
        self.assertEqual(job.state, 'done')
        bodies = self._bot_messages(session).mapped('body')
        self.assertEqual(len(bodies), 2)
        self.assertIn('第一則', bodies[0])
        self.assertIn('<strong>第二則</strong>', bodies[1])
        self.assertEqual(job.reply_message_id, self._bot_messages(session)[0])

    @patch(POST)
    def test_skip_posts_nothing(self, mock_post):
        mock_post.return_value = _json_response({'skip': True})
        session, _guest, message = self._guest_session(self.n8n)
        job = self._run(session, message)
        self.assertEqual(job.state, 'done')
        self.assertIn('skip', job.error)
        self.assertFalse(self._bot_messages(session))
        log = self.env['llm.api.log'].search([('discuss_channel_id', '=', session.id)])
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.backend_type, 'n8n')
        self.assertEqual(log.http_status, 200)

    @patch(POST)
    def test_error_posts_error_message(self, mock_post):
        mock_post.return_value = _json_response({'error': 'agent_failed'}, status=502)
        self.n8n.write({'ai_error_message': '後端暫時無法回覆'})
        session, _guest, message = self._guest_session(self.n8n)
        with mute_logger(CHANNEL_LOGGER, 'odoo.addons.im_livechat_ai.models.livechat_ai_job'):
            job = self._run(session, message)
        self.assertEqual(job.state, 'failed')
        self.assertIn('HTTP 502', job.error)
        self.assertIn('後端暫時無法回覆', self._bot_messages(session).body)
        log = self.env['llm.api.log'].search([('discuss_channel_id', '=', session.id)])
        self.assertEqual(log.status, 'error')
        self.assertEqual(log.http_status, 502)
        self.assertNotIn('agent_failed', log.error_message)


class TestPerChannelLogs(BackendCase):
    """Content logging and retention are per channel (replacing the system
    parameters of 18.0.1.1.0)."""

    @patch(POST)
    def test_payload_only_for_channel_with_logging_on(self, mock_post):
        mock_post.return_value = ok_response('回覆內容')
        self.channel.write({'ai_log_payload': True})
        on_session, _g, _m = self._guest_session(self.channel, text='保固多久？')
        call_backend(self.channel, session=on_session)
        mock_post.return_value = _json_response({'reply': 'n8n 回覆'})
        off_session, _g, _m = self._guest_session(self.n8n, text='運費多少？')
        call_backend(self.n8n, session=off_session)
        on_log = self.env['llm.api.log'].search([('discuss_channel_id', '=', on_session.id)])
        off_log = self.env['llm.api.log'].search([('discuss_channel_id', '=', off_session.id)])
        self.assertIn('保固多久', on_log.request_payload)
        self.assertIn('回覆內容', on_log.response_payload)
        self.assertNotIn(TEST_API_KEY, on_log.request_payload)
        self.assertFalse(off_log.request_payload)
        self.assertFalse(off_log.response_payload)
        self.assertEqual(off_log.backend_type, 'n8n')
        # Turning logging off erases the stored content of that channel only.
        other = self.env['llm.api.log'].create({
            'status': 'success', 'livechat_channel_id': self.n8n.id, 'request_payload': '{"x": 1}',
        })
        self.channel.write({'ai_log_payload': False})
        self.assertFalse(on_log.request_payload)
        self.assertFalse(on_log.response_payload)
        self.assertEqual(other.request_payload, '{"x": 1}')

    def test_retention_per_channel(self):
        Log = self.env['llm.api.log']
        Job = self.env['im_livechat.ai.job']
        self.channel.write({'ai_log_retention_days': 3})
        self.n8n.write({'ai_log_retention_days': 30})
        now = fields.Datetime.now()
        logs = {
            'a_2': Log.create({'status': 'success', 'livechat_channel_id': self.channel.id,
                               'timestamp': now - timedelta(days=2)}),
            'a_5': Log.create({'status': 'success', 'livechat_channel_id': self.channel.id,
                               'timestamp': now - timedelta(days=5)}),
            'b_20': Log.create({'status': 'success', 'livechat_channel_id': self.n8n.id,
                                'timestamp': now - timedelta(days=20)}),
            'b_40': Log.create({'status': 'success', 'livechat_channel_id': self.n8n.id,
                                'timestamp': now - timedelta(days=40)}),
            'none_10': Log.create({'status': 'success', 'timestamp': now - timedelta(days=10)}),
            'none_20': Log.create({'status': 'success', 'timestamp': now - timedelta(days=20)}),
        }
        session_a, _g, _m = self._guest_session(self.channel)
        session_b, _g, _m = self._guest_session(self.n8n)
        job_a = Job.create({'discuss_channel_id': session_a.id, 'livechat_channel_id': self.channel.id,
                            'state': 'done'})
        job_b = Job.create({'discuss_channel_id': session_b.id, 'livechat_channel_id': self.n8n.id,
                            'state': 'done'})
        self.env.cr.execute("UPDATE im_livechat_ai_job SET create_date = %s WHERE id IN %s",
                            (now - timedelta(days=5), (job_a.id, job_b.id)))
        self.env.invalidate_all()
        self.assertEqual(Log._cleanup_old_logs(), 3)
        self.assertEqual({k for k, log in logs.items() if log.exists()}, {'a_2', 'b_20', 'none_10'})
        self.assertFalse(job_a.exists())
        self.assertTrue(job_b.exists())

    def test_retention_bounds(self):
        with self.assertRaises(ValidationError):
            self.channel.write({'ai_log_retention_days': 0})
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_log_retention_days = 0 WHERE id = %s",
                            (self.channel.id,))
        self.env.invalidate_all()
        self.assertEqual(self.env['llm.api.log']._retention_by_channel()[self.channel.id], 14)

    def test_cron_uses_cleanup(self):
        cron = self.env.ref('im_livechat_ai.ir_cron_cleanup_llm_api_logs')
        self.assertIn('_cleanup_old_logs', cron.code)
        old = self.env['llm.api.log'].create({
            'status': 'success', 'livechat_channel_id': self.channel.id,
            'timestamp': fields.Datetime.now() - timedelta(days=15),
        })
        cron.method_direct_trigger()
        self.assertFalse(old.exists())


def _load_migration(version, name):
    path = f"{get_module_path('im_livechat_ai')}/migrations/{version}/{name}.py"
    spec = importlib.util.spec_from_file_location(
        f'im_livechat_ai_mig_{version.replace(".", "_")}_{name.replace("-", "_")}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration18012(TransactionCase):
    """18.0.1.1.0 -> 18.0.1.2.0: system parameters move to every channel."""

    DUMMY_URL = 'https://litellm.dummy.test/v1/chat/completions/'
    DUMMY_KEY = 'dummy-migration-key'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.post = _load_migration('18.0.1.2.0', 'post-migrate')
        Channel = cls.env['im_livechat.channel']
        cls.on = Channel.create({'name': 'Legacy On', 'ai_model': 'chatgpt-pool/gpt-6-sol'})
        cls.off = Channel.create({'name': 'Legacy Off', 'ai_model': 'MiniMax-M1'})
        # 18.0.1.1.0 state: AI on, no connection on the channel.
        cls.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = (id = %s), "
                           "ai_endpoint_url = NULL, ai_api_token_secret = NULL, ai_log_payload = NULL, "
                           "ai_log_retention_days = NULL", (cls.on.id,))
        cls.env.invalidate_all()

    def _set_params(self, **params):
        ICP = self.env['ir.config_parameter'].sudo()
        for key, value in params.items():
            ICP.set_param('im_livechat_ai.' + key, value)

    def _params(self):
        self.env.flush_all()
        self.env.cr.execute("SELECT count(*) FROM ir_config_parameter WHERE key LIKE 'im_livechat_ai.%%'")
        return self.env.cr.fetchone()[0]

    def test_params_copied_to_every_channel(self):
        self._set_params(base_url=self.DUMMY_URL, api_key=self.DUMMY_KEY, log_payload='True',
                         log_retention_days='21', model_cache='["a"]')
        with self.assertLogs(self.post.__name__, level='WARNING') as logs:
            self.post.migrate(self.env.cr, '18.0.1.1.0')
        self.env.invalidate_all()
        self.assertEqual(self._params(), 0)
        for channel in (self.on, self.off):
            self.assertEqual(channel.ai_backend_type, 'openai')
            self.assertEqual(channel.ai_endpoint_url, 'https://litellm.dummy.test/v1')
            self.assertEqual(channel._ai_get_token(), self.DUMMY_KEY)
            self.assertTrue(channel.ai_log_payload)
            self.assertEqual(channel.ai_log_retention_days, 21)
        # AI state and model are kept: the enabled channel keeps working.
        self.assertTrue(self.on.ai_enabled)
        self.assertFalse(self.off.ai_enabled)
        self.assertEqual(self.off.ai_model, 'MiniMax-M1')
        self.assertTrue(self.on.ai_connection_ready)
        # The summary has counts only, never the URL or the key.
        output = '\n'.join(logs.output)
        self.assertIn('access token copied to', output)
        self.assertNotIn(self.DUMMY_KEY, output)
        self.assertNotIn('litellm.dummy.test', output)

    def test_idempotent_and_never_overwrites(self):
        self._set_params(base_url=self.DUMMY_URL, api_key=self.DUMMY_KEY)
        with mute_logger(self.post.__name__):
            self.post.migrate(self.env.cr, '18.0.1.1.0')
        self.env.invalidate_all()
        # An administrator changes one channel afterwards.
        self.off.write({'ai_backend_type': 'n8n', 'ai_endpoint_url': 'https://n8n.test/webhook/x',
                        'ai_api_token': 'dummy-n8n-secret'})
        # Parameters re-created (e.g. by hand) are copied only where empty.
        self._set_params(base_url='https://other.test/v1', api_key='dummy-other')
        with mute_logger(self.post.__name__):
            self.post.migrate(self.env.cr, '18.0.1.1.0')
            self.post.migrate(self.env.cr, '18.0.1.1.0')
        self.env.invalidate_all()
        self.assertEqual(self.off.ai_backend_type, 'n8n')
        self.assertEqual(self.off.ai_endpoint_url, 'https://n8n.test/webhook/x')
        self.assertEqual(self.off._ai_get_token(), 'dummy-n8n-secret')
        self.assertEqual(self.on.ai_endpoint_url, 'https://litellm.dummy.test/v1')
        self.assertEqual(self.on._ai_get_token(), self.DUMMY_KEY)
        self.assertEqual(self._params(), 0)

    def test_without_params_nothing_is_enabled(self):
        """Production: no parameters, AI off everywhere -> nothing changes."""
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = FALSE")
        self.env.invalidate_all()
        with self.assertLogs(self.post.__name__, level='WARNING') as logs:
            self.post.migrate(self.env.cr, '18.0.1.1.0')
        self.env.invalidate_all()
        for channel in (self.on, self.off):
            self.assertFalse(channel.ai_enabled)
            self.assertFalse(channel.ai_endpoint_url)
            self.assertFalse(channel._ai_get_token())
            self.assertEqual(channel.ai_backend_type, 'openai')
            self.assertFalse(channel.ai_log_payload)
            self.assertEqual(channel.ai_log_retention_days, 14)
        self.assertIn('0 im_livechat_ai.* system parameter(s) deleted', '\n'.join(logs.output))

    def test_key_without_url_uses_the_1_1_0_default(self):
        self._set_params(api_key=self.DUMMY_KEY)
        with mute_logger(self.post.__name__):
            self.post.migrate(self.env.cr, '18.0.1.1.0')
        self.env.invalidate_all()
        self.assertEqual(self.on.ai_endpoint_url, self.post.LEGACY_DEFAULT_BASE_URL)
        self.assertTrue(self.on.ai_connection_ready)

    def test_fresh_install_is_noop(self):
        self._set_params(api_key=self.DUMMY_KEY)
        self.post.migrate(self.env.cr, None)
        self.assertEqual(self._params(), 1)


class TestMigration18011(TransactionCase):
    """Upgrade from 18.0.1.0.x (18.0.1.1.0 scripts, still shipped): legacy
    credentials wiped, all channels disabled."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pre = _load_migration('18.0.1.1.0', 'pre-migrate')
        cls.post = _load_migration('18.0.1.1.0', 'post-migrate')
        Channel = cls.env['im_livechat.channel']
        cls.ch_on = Channel.create({**TEST_BACKEND, 'name': 'Legacy On', 'ai_enabled': True})
        cls.ch_off = Channel.create({'name': 'Legacy Off', 'ai_enabled': False})
        cls.env.cr.execute("UPDATE im_livechat_channel SET ai_model = 'MiniMax-M1' WHERE id = %s",
                           (cls.ch_on.id,))
        cls.env.invalidate_all()

    def _add_legacy_columns(self):
        cr = self.env.cr
        for column in ('ai_api_key', 'ai_api_base_url'):
            cr.execute(f"ALTER TABLE im_livechat_channel ADD COLUMN IF NOT EXISTS {column} varchar")
        cr.execute("UPDATE im_livechat_channel SET ai_api_key = 'dummy-legacy-key', "
                   "ai_api_base_url = 'https://api.legacy.example/v1'")

    def test_pre_migrate_wipes_credentials(self):
        self._add_legacy_columns()
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        self.env.cr.execute("""SELECT count(*) FROM im_livechat_channel
                                WHERE ai_api_key IS NOT NULL OR ai_api_base_url IS NOT NULL""")
        self.assertEqual(self.env.cr.fetchone()[0], 0)

    def test_post_migrate_drops_columns_and_disables_ai(self):
        self._add_legacy_columns()
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        with self.assertLogs(self.post.__name__, level='WARNING'):
            self.post.migrate(self.env.cr, '18.0.1.0.1')
        self.env.cr.execute("SELECT count(*) FROM im_livechat_channel WHERE ai_enabled")
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.env.invalidate_all()
        self.assertFalse(self.ch_on.ai_enabled)
        self.assertEqual(self.ch_on.ai_model, 'MiniMax-M1')

    def test_upgrade_from_1_0_runs_both_versions(self):
        """1.0.x -> 1.2.0 runs the 1.1.0 then the 1.2.0 scripts: AI off,
        no connection invented, no parameter left."""
        post12 = _load_migration('18.0.1.2.0', 'post-migrate')
        self._add_legacy_columns()
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        with mute_logger(self.post.__name__, post12.__name__):
            self.post.migrate(self.env.cr, '18.0.1.0.1')
            post12.migrate(self.env.cr, '18.0.1.0.1')
        self.env.invalidate_all()
        self.assertFalse(self.ch_off.ai_endpoint_url)
        self.assertFalse(self.ch_on.ai_enabled)
        self.env.cr.execute("SELECT count(*) FROM ir_config_parameter WHERE key LIKE 'im_livechat_ai.%%'")
        self.assertEqual(self.env.cr.fetchone()[0], 0)


class TestRequestTimeout(BackendCase):
    """ai_request_timeout: seconds a chat call waits, per channel (live test:
    the n8n advisor took 112 s, Odoo gave up at 90 s)."""

    def _timeout(self, mock_post, channel, response):
        mock_post.return_value = response
        call_backend(channel)
        return mock_post.call_args.kwargs['timeout']

    @patch(POST)
    def test_defaults_when_zero_or_empty(self, mock_post):
        for value in (0, False):
            self.channel.ai_request_timeout = value
            self.n8n.ai_request_timeout = value
            self.assertEqual(self._timeout(mock_post, self.channel, ok_response()), 45)
            self.assertEqual(self._timeout(mock_post, self.n8n, _json_response({'skip': True})), 120)

    @patch(POST)
    def test_channel_value_is_used(self, mock_post):
        self.n8n.ai_request_timeout = 180
        self.channel.ai_request_timeout = 20
        self.assertEqual(self._timeout(mock_post, self.n8n, _json_response({'skip': True})), 180)
        self.assertEqual(self._timeout(mock_post, self.channel, ok_response()), 20)

    @patch(POST)
    def test_out_of_range_stored_value_is_clamped(self, mock_post):
        """A value written around the constraint never reaches requests."""
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_request_timeout = 5000 WHERE id = %s",
                            (self.n8n.id,))
        self.n8n.invalidate_recordset(['ai_request_timeout'])
        self.assertEqual(self._timeout(mock_post, self.n8n, _json_response({'skip': True})), 300)

    def test_range_constraint(self):
        for value in (0, 10, 112, 300):
            self.channel.ai_request_timeout = value
        for value in (5, 9, 301, -1):
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.channel.ai_request_timeout = value
                self.channel.flush_recordset()

    def test_manager_can_set_it(self):
        self.channel.with_user(self.manager).ai_request_timeout = 150
        self.assertEqual(self.channel.ai_request_timeout, 150)

    @patch(POST)
    def test_connection_test_timeouts_unchanged(self, mock_post):
        self.channel.ai_request_timeout = 250
        self.n8n.ai_request_timeout = 250
        mock_post.return_value = ok_response()
        self.channel.with_user(self.admin).action_test_ai_connection()
        self.assertEqual(mock_post.call_args.kwargs['timeout'], LLM_REQUEST_TIMEOUT)
        mock_post.return_value = _json_response({'skip': True})
        self.n8n.with_user(self.admin).action_test_ai_connection()
        self.assertEqual(mock_post.call_args.kwargs['timeout'], N8N_TEST_TIMEOUT)

    def test_job_thresholds_cover_the_longest_run(self):
        from odoo.addons.im_livechat_ai.models import livechat_ai_job as jobs
        longest = (LLM_MAX_RETRIES + 1) * MAX_REQUEST_TIMEOUT + sum(LLM_RETRY_BACKOFF)
        self.assertEqual(jobs.MAX_RUN_SECONDS, longest)
        self.assertGreater(jobs.JOB_MAX_AGE_MINUTES * 60, longest)
        self.assertEqual(MAX_REQUEST_TIMEOUT, 300)
