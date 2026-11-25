# -*- coding: utf-8 -*-
"""AI connections (「AI 連線」, 18.0.2.0.0): write-only key, F4, readiness,
defaults, Test Connection and access. All HTTP is mocked; no real key."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger

from .common import TEST_API_KEY, TEST_BACKEND, TEST_BASE_URL, add_legacy_channel_columns, make_ai_channel, \
    make_assistant, make_connection

GET = 'odoo.addons.im_livechat_ai.models.ai_connection.requests.get'
CONNECTION_POST = 'odoo.addons.im_livechat_ai.models.ai_connection.requests.post'
POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
SENTINEL = 'sk-sentinel-never-shown-0123456789'


def _response(data=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = data if data is not None else {}
    response.text = json.dumps(data or {})
    return response


class ConnectionCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, login='conn_admin', name='Admin',
                                  groups='base.group_user,base.group_system')
        cls.manager = new_test_user(cls.env, login='conn_manager', name='Manager',
                                    groups='base.group_user,im_livechat.im_livechat_group_manager')
        cls.operator = new_test_user(cls.env, login='conn_operator', name='Operator',
                                     groups='base.group_user,im_livechat.im_livechat_group_user')
        cls.portal = new_test_user(cls.env, login='conn_portal', name='Portal', groups='base.group_portal')
        cls.connection = make_connection(cls.env, name='LiteLLM', api_key=SENTINEL, endpoint_url=TEST_BASE_URL,
                                         model='chatgpt-pool/gpt-6-sol')
        cls.assistant = make_assistant(cls.env, cls.connection, name='官網助理', system_prompt='秘密提示')


class TestConnection(ConnectionCase):

    def test_key_is_write_only(self):
        connection = self.connection.with_user(self.admin)
        self.assertTrue(connection.api_key_set)
        self.assertFalse(connection.read(['api_key'])[0]['api_key'])
        rows = connection.search_read([('id', '=', connection.id)])
        self.assertNotIn(SENTINEL, json.dumps(rows, default=str))
        exported = connection.export_data(['name', 'api_key', 'api_key_set', 'endpoint_url'])['datas']
        self.assertNotIn(SENTINEL, json.dumps(exported, default=str))
        result = connection.web_save({'api_key': 'dummy-typed-key'}, {'api_key': {}, 'api_key_set': {}})
        self.assertFalse(result[0]['api_key'])
        self.assertTrue(result[0]['api_key_set'])
        self.assertEqual(self.connection._get_api_key(), 'dummy-typed-key')
        self.assertNotIn('api_key_secret', self.connection._fields)

    def test_empty_input_keeps_the_key_and_remove_button(self):
        self.connection.write({'api_key': ''})
        self.connection.write({'api_key': False, 'description': 'x'})
        self.assertEqual(self.connection._get_api_key(), SENTINEL)
        self.assertIs(self.connection.with_user(self.admin).action_clear_api_key(), True)
        self.assertFalse(self.connection.api_key_set)
        self.assertEqual(self.connection.readiness, 'blocked')

    def test_key_dropped_when_host_or_type_changes(self):
        self.connection.write({'endpoint_url': TEST_BASE_URL + '/'})  # same origin
        self.assertTrue(self.connection.api_key_set)
        with mute_logger('odoo.addons.im_livechat_ai.models.ai_connection'):
            self.connection.write({'endpoint_url': 'https://other.example/v1'})
        self.assertFalse(self.connection.api_key_set)
        self.connection.write({'api_key': 'dummy-2'})
        with mute_logger('odoo.addons.im_livechat_ai.models.ai_connection'):
            self.connection.write({'backend_type': 'n8n'})
        self.assertFalse(self.connection.api_key_set)
        # a new key in the same write is kept
        self.connection.write({'backend_type': 'openai', 'endpoint_url': 'https://third.example/v1',
                               'api_key': 'dummy-3'})
        self.assertEqual(self.connection._get_api_key(), 'dummy-3')

    def test_rotating_the_key_clears_the_legacy_channel_copy(self):
        """A rollback to 18.0.1.3.5 never brings back a rotated or removed key."""
        cr = self.env.cr
        add_legacy_channel_columns(cr)
        channel = self.env['im_livechat.channel'].create({'name': 'Legacy', 'ai_assistant_id': self.assistant.id})
        other = self.env['im_livechat.channel'].create({'name': 'Other'})
        self.env.flush_all()
        cr.execute("UPDATE im_livechat_channel SET ai_api_token_secret = 'dummy-old' WHERE id IN %s",
                   ((channel.id, other.id),))
        self.connection.write({'api_key': 'dummy-rotated'})
        cr.execute("SELECT id, ai_api_token_secret IS NULL FROM im_livechat_channel WHERE id IN %s ORDER BY id",
                   ((channel.id, other.id),))
        self.assertEqual(dict(cr.fetchall()), {channel.id: True, other.id: False})
        # a channel moved to an assistant of another connection loses its copy too
        cr.execute("UPDATE im_livechat_channel SET ai_api_token_secret = 'dummy-old' WHERE id = %s", (other.id,))
        other.ai_assistant_id = self.assistant
        cr.execute("SELECT ai_api_token_secret IS NULL FROM im_livechat_channel WHERE id = %s", (other.id,))
        self.assertTrue(cr.fetchone()[0])

    @patch(POST)
    def test_key_with_whitespace_is_sent_stripped(self, mock_post):
        mock_post.return_value = _response({'choices': [{'message': {'content': 'ok'}}]})
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_ai_connection SET api_key_secret = %s WHERE id = %s",
                            ('  %s\n' % SENTINEL, self.connection.id))
        channel = self.env['im_livechat.channel'].create({'name': 'Strip', 'ai_assistant_id': self.assistant.id})
        session = self.env['discuss.channel'].create({
            'name': 'S', 'channel_type': 'livechat', 'livechat_channel_id': channel.id,
            'livechat_operator_id': self.env.ref('base.partner_admin').id})
        self.env['mail.message'].create({'body': 'hi', 'model': 'discuss.channel', 'res_id': session.id,
                                         'message_type': 'comment', 'author_id': False})
        channel._generate_ai_reply(session, session._ai_conversation(channel))
        self.assertEqual(mock_post.call_args.kwargs['headers']['Authorization'], 'Bearer ' + SENTINEL)

    def test_customer_safe_f4(self):
        """F4: a LiteLLM connection is always safe for customers; n8n only once
        ticked, cleared when its URL or type changes."""
        self.assertTrue(self.connection.customer_safe)
        with self.assertRaises(ValidationError):
            self.connection.write({'customer_safe': False})
        n8n = make_connection(self.env, name='n8n', backend_type='n8n', endpoint_url='https://n8n.example/webhook/a')
        self.assertFalse(n8n.customer_safe)
        n8n.customer_safe = True
        with mute_logger('odoo.addons.im_livechat_ai.models.ai_connection'):
            n8n.write({'endpoint_url': 'https://n8n.example/webhook/b'})
        self.assertFalse(n8n.customer_safe)
        n8n.write({'backend_type': 'openai'})
        self.assertTrue(n8n.customer_safe)
        self.assertTrue(n8n.copy().customer_safe)
        self.assertIn('(copy)', n8n.copy().name)

    def test_readiness(self):
        self.assertEqual(self.connection.readiness, 'ok')
        cases = (
            ({'model': False}, 'Set the model'),
            ({'endpoint_url': False}, 'Set the endpoint URL'),
            ({'active': False}, 'archived'),
        )
        for vals, reason in cases:
            with self.subTest(vals=vals):
                connection = make_connection(self.env, name='case', endpoint_url=TEST_BASE_URL, api_key=SENTINEL,
                                             model='m')
                self.assertEqual(connection.readiness, 'ok')
                connection.write(vals)
                self.assertEqual(connection.readiness, 'blocked')
                self.assertIn(reason, connection.readiness_reason)
                self.assertNotIn(TEST_BASE_URL, connection.readiness_reason)
                self.assertNotIn(SENTINEL, connection.readiness_reason)
        n8n = make_connection(self.env, name='n8n', backend_type='n8n', endpoint_url='https://n8n.example/hook')
        self.assertEqual(n8n.readiness, 'ok', 'n8n needs no key and no model')
        Connection = self.env['im_livechat.ai.connection'].with_context(active_test=False)
        self.assertIn(self.connection, Connection.search([('readiness', '=', 'ok')]))
        blocked = make_connection(self.env, name='blocked', endpoint_url=TEST_BASE_URL)
        self.assertIn(blocked, Connection.search([('readiness', '=', 'blocked')]))
        self.assertNotIn(blocked, Connection.search([('readiness', '!=', 'blocked')]))

    def test_new_connection_timeouts_through_the_form(self):
        """The form's defaults fill in 45 s; choosing n8n swaps it for 120 s."""
        with Form(self.env['im_livechat.ai.connection']) as form:
            form.name = 'n8n advisor'
            form.backend_type = 'n8n'
            form.endpoint_url = 'https://n8n.example/webhook/advisor/chat'
        self.assertEqual(form.record.timeout_seconds, 120)
        self.assertFalse(form.record.customer_safe)
        with Form(self.env['im_livechat.ai.connection']) as form:
            form.name = 'LiteLLM'
            form.endpoint_url = TEST_BASE_URL
            form.model = 'm'
        self.assertEqual(form.record.timeout_seconds, 45)
        self.assertTrue(form.record.customer_safe)
        # created without a timeout through RPC: by type
        self.assertEqual(make_connection(self.env, name='x', backend_type='n8n').timeout_seconds, 120)
        self.assertEqual(make_connection(self.env, name='y', backend_type='n8n', timeout_seconds=200).timeout_seconds,
                         200)


class TestConnectionTest(ConnectionCase):
    """Test Connection: LiteLLM GET /models (no model run, no tokens); n8n ping."""

    @patch(GET)
    def test_litellm_models_list(self, mock_get):
        mock_get.return_value = _response({'data': [{'id': 'chatgpt-pool/gpt-6-sol'}]})
        result = self.connection.with_user(self.admin).action_test_connection()
        self.assertEqual(result['params']['type'], 'success')
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], TEST_BASE_URL + '/models')
        self.assertEqual(kwargs['headers'], {'Authorization': 'Bearer ' + SENTINEL})
        self.assertEqual(kwargs['timeout'], (10, 15))
        self.assertIs(kwargs['allow_redirects'], False)
        self.assertNotIn(SENTINEL, json.dumps(result))
        mock_get.return_value = _response({'data': [{'id': 'other'}], 'detail': 'upstream-detail'})
        result = self.connection.with_user(self.admin).action_test_connection()
        self.assertEqual(result['params']['type'], 'warning')
        self.assertIn('chatgpt-pool/gpt-6-sol', result['params']['message'])
        self.assertNotIn('upstream-detail', result['params']['message'])

    @patch(GET)
    def test_admin_only(self, mock_get):
        for user in (self.manager, self.operator, self.portal):
            with self.assertRaises(AccessError):
                self.connection.with_user(user).action_test_connection()
        with self.assertRaises(AccessError):
            self.assistant.with_user(self.manager).action_test_connection()
        mock_get.assert_not_called()

    @patch(CONNECTION_POST)
    def test_n8n_ping_per_origin(self, mock_post):
        mock_post.return_value = _response({})
        n8n = make_connection(self.env, name='n8n', backend_type='n8n', endpoint_url='https://n8n.example/hook',
                              api_key='dummy-signing')
        assistant = make_assistant(self.env, n8n, name='n8n 助理')
        channel = self.env['im_livechat.channel'].create({'name': 'Ping Channel', 'ai_assistant_id': assistant.id})
        n8n.action_test_connection()
        body = mock_post.call_args.kwargs['data']
        self.assertEqual(json.loads(body), {'event': 'ping', 'connection': {'id': n8n.id, 'name': 'n8n'},
                                            'action': 'loadPreviousSession',
                                            'sessionId': 'woow-ai-ping-connection-%s' % n8n.id})
        headers = mock_post.call_args.kwargs['headers']
        expected = 'sha256=' + hmac.new(b'dummy-signing', headers['X-Woow-Timestamp'].encode() + b'.' + body,
                                        hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Woow-Signature'], expected)
        assistant.action_test_connection()
        ping = json.loads(mock_post.call_args.kwargs['data'])
        self.assertEqual(ping['assistant'], {'id': assistant.id, 'name': 'n8n 助理'})
        self.assertEqual(ping['sessionId'], 'woow-ai-ping-assistant-%s' % assistant.id)
        self.assertNotIn('contact', ping)
        channel.action_test_ai_connection()
        ping = json.loads(mock_post.call_args.kwargs['data'])
        self.assertEqual(ping['contact'], {'id': channel.id, 'name': 'Ping Channel'})
        self.assertEqual(ping['sessionId'], 'woow-ai-ping-%s' % channel.id)
        # unsigned without a secret
        n8n.action_clear_api_key()
        n8n.action_test_connection()
        self.assertNotIn('X-Woow-Signature', mock_post.call_args.kwargs['headers'])
        mock_post.return_value = _response({'error': 'upstream-secret-detail'}, status=401)
        result = n8n.action_test_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('HTTP 401', result['params']['message'])
        self.assertNotIn('upstream-secret-detail', result['params']['message'])


class TestAccess(ConnectionCase):
    """Q12 A in place and Q13 ②: administrators own connections, assistants, jobs,
    logs and usage; Live Chat managers read assistant names and pick them."""

    def test_manager(self):
        assistant = self.assistant.with_user(self.manager)
        values = assistant.read(['name', 'description', 'active', 'sequence', 'partner_id', 'customer_safe',
                                 'livechat_selectable'])[0]
        self.assertEqual(values['name'], '官網助理')
        self.assertTrue(values['customer_safe'])
        for fname in ('system_prompt', 'connection_id', 'error_message', 'max_history', 'livechat_available'):
            with self.assertRaises(AccessError, msg=fname):
                assistant.read([fname])
        with self.assertRaises(AccessError):
            assistant.write({'name': 'renamed'})
        with self.assertRaises(AccessError):
            self.env['im_livechat.ai.assistant'].with_user(self.manager).create(
                {'name': 'x', 'connection_id': self.connection.id})
        with self.assertRaises(AccessError):
            assistant.unlink()
        for model in ('im_livechat.ai.connection', 'im_livechat.ai.job', 'llm.api.log', 'im_livechat.ai.usage.month'):
            with self.assertRaises(AccessError, msg=model):
                self.env[model].with_user(self.manager).search([])
        # the picker's domain works for a manager
        self.assertIn(self.assistant, self.env['im_livechat.ai.assistant'].with_user(self.manager).search(
            [('livechat_selectable', '=', True)]))

    def test_operator_portal_public_read_nothing(self):
        public = self.env.ref('base.public_user')
        for user in (self.operator, self.portal, public):
            for model in ('im_livechat.ai.connection', 'im_livechat.ai.assistant', 'im_livechat.ai.job',
                          'llm.api.log', 'im_livechat.ai.usage.month'):
                with self.assertRaises(AccessError, msg=(user.login, model)):
                    self.env[model].with_user(user).search([])

    def test_admin_reads_everything(self):
        for model in ('im_livechat.ai.connection', 'im_livechat.ai.assistant', 'im_livechat.ai.job',
                      'llm.api.log', 'im_livechat.ai.usage.month'):
            self.env[model].with_user(self.admin).search([])
        values = self.assistant.with_user(self.admin).read(['system_prompt', 'connection_id'])[0]
        self.assertEqual(values['system_prompt'], '秘密提示')
        connection = self.connection.with_user(self.admin)
        self.assertEqual(connection.endpoint_url, TEST_BASE_URL)
        self.assertFalse(connection.api_key)

    def test_menus_and_actions_for_administrators_only(self):
        manager_menus = self.env['ir.ui.menu'].with_user(self.manager)._visible_menu_ids()
        admin_menus = self.env['ir.ui.menu'].with_user(self.admin)._visible_menu_ids()
        for xmlid in ('menu_im_livechat_ai_connection', 'menu_im_livechat_ai_assistant', 'im_livechat_ai_job_menu',
                      'llm_api_log_menu', 'menu_im_livechat_ai_usage_month'):
            menu = self.env.ref('im_livechat_ai.%s' % xmlid)
            self.assertNotIn(menu.id, manager_menus, xmlid)
            self.assertEqual(menu.groups_id, self.env.ref('base.group_system'), xmlid)
        for xmlid in ('im_livechat_ai_job_action', 'llm_api_log_action', 'action_im_livechat_ai_connection',
                      'action_im_livechat_ai_assistant', 'action_im_livechat_ai_usage_month'):
            self.assertEqual(self.env.ref('im_livechat_ai.%s' % xmlid).groups_id,
                             self.env.ref('base.group_system'), xmlid)
        self.assertTrue(admin_menus)

    def test_menu_order_and_labels(self):
        """Discuss AI's order: AI Connections, AI Assistants, Reply Jobs, Call Logs, AI Usage."""
        names = [self.env.ref('im_livechat_ai.%s' % xmlid).with_context(lang='en_US').name for xmlid in (
            'menu_im_livechat_ai_connection', 'menu_im_livechat_ai_assistant', 'im_livechat_ai_job_menu',
            'llm_api_log_menu', 'menu_im_livechat_ai_usage_month')]
        self.assertEqual(names, ['AI Connections', 'AI Assistants', 'Reply Jobs', 'Call Logs', 'AI Usage'])
        sequences = [self.env.ref('im_livechat_ai.%s' % xmlid).sequence for xmlid in (
            'menu_im_livechat_ai_connection', 'menu_im_livechat_ai_assistant', 'im_livechat_ai_job_menu',
            'llm_api_log_menu', 'menu_im_livechat_ai_usage_month')]
        self.assertEqual(sequences, sorted(sequences))

    def test_channel_ai_fields_for_managers(self):
        channel = make_ai_channel(self.env, {**TEST_BACKEND, 'name': 'Managed', 'ai_enabled': True})
        values = channel.with_user(self.manager).read(['ai_state', 'ai_assistant_id'])[0]
        self.assertEqual(values['ai_state'], 'ok')
        for user in (self.operator, self.portal):
            with self.assertRaises(AccessError):
                channel.with_user(user).read(['ai_assistant_id'])
        self.assertNotIn(TEST_API_KEY, json.dumps(channel.with_user(self.manager).read([])[0], default=str))
