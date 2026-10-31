# -*- coding: utf-8 -*-
"""Package B (system-level LiteLLM) and F (log privacy) tests.

All HTTP is mocked; no real key is used.
"""

import importlib.util
import json
import logging
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import SQL, mute_logger, sql

from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ICP_API_KEY,
    ICP_BASE_URL,
    ICP_LOG_PAYLOAD,
    ICP_LOG_RETENTION_DAYS,
    ICP_MODEL_CACHE,
    LLMNotConfigured,
)

from .common import TEST_API_KEY, TEST_BASE_URL, livechat_operator, setup_litellm

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
GET = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.get'

SECRET_KEY = 'sk-very-secret-system-key'


def _ok_chat_response(content='OK'):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': {'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
    }
    response.raise_for_status = MagicMock()
    return response


def _http_error_response(status, body):
    response = MagicMock()
    response.status_code = status
    response.text = body
    response.raise_for_status.side_effect = requests.HTTPError(f'{status} Error', response=response)
    return response


class LivechatAiUsersCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env, api_key=SECRET_KEY)
        cls.manager = new_test_user(
            cls.env, login='lc_ai_manager', name='Livechat Manager',
            groups='base.group_user,im_livechat.im_livechat_group_manager',
        )
        cls.lc_user = new_test_user(
            cls.env, login='lc_ai_user', name='Livechat User',
            groups='base.group_user,im_livechat.im_livechat_group_user',
        )
        cls.admin = new_test_user(
            cls.env, login='lc_ai_admin', name='System Admin',
            groups='base.group_user,base.group_system,im_livechat.im_livechat_group_manager',
        )
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'LiteLLM Channel',
            'ai_enabled': True,
            'ai_model': DEFAULT_MODEL,
        })
        cls.session = cls.env['discuss.channel'].create({
            'name': 'LiteLLM Session',
            'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id,
            'livechat_channel_id': cls.channel.id,
        })
        cls.visitor = cls.env['res.partner'].create({'name': 'LiteLLM Visitor'})

    def _add_visitor_msg(self, body='你好，請問價格？'):
        self.env['mail.message'].create({
            'body': body,
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'comment',
            'author_id': self.visitor.id,
        })


class TestLiteLLMPermissions(LivechatAiUsersCase):
    """Non-admins must not read or write the connection settings by any route."""

    def test_channel_has_no_credential_fields(self):
        Channel = self.env['im_livechat.channel']
        self.assertNotIn('ai_api_key', Channel._fields)
        self.assertNotIn('ai_api_base_url', Channel._fields)
        for user in (self.manager, self.lc_user, self.admin):
            fields_info = Channel.with_user(user).fields_get()
            self.assertNotIn('ai_api_key', fields_info)
            self.assertNotIn('ai_api_base_url', fields_info)

    def test_non_admin_cannot_read_config_parameters(self):
        for user in (self.manager, self.lc_user):
            ICP = self.env['ir.config_parameter'].with_user(user)
            with self.assertRaises(AccessError):
                ICP.get_param(ICP_API_KEY)
            with self.assertRaises(AccessError):
                ICP.search_read([('key', '=', ICP_API_KEY)], ['value'])

    def test_non_admin_cannot_write_config_parameters(self):
        for user in (self.manager, self.lc_user):
            ICP = self.env['ir.config_parameter'].with_user(user)
            with self.assertRaises(AccessError):
                ICP.set_param(ICP_BASE_URL, 'https://attacker.example/v1')
            with self.assertRaises(AccessError):
                ICP.create({'key': ICP_BASE_URL + '.x', 'value': 'https://attacker.example/v1'})
        self.assertEqual(self.env['ir.config_parameter'].get_param(ICP_BASE_URL), TEST_BASE_URL)

    def test_non_admin_cannot_use_settings(self):
        for user in (self.manager, self.lc_user):
            Settings = self.env['res.config.settings'].with_user(user)
            with self.assertRaises(AccessError):
                Settings.create({'livechat_ai_base_url': 'https://attacker.example/v1'})
            with self.assertRaises(AccessError):
                Settings.create({})

    def test_settings_fields_are_system_only(self):
        Settings = self.env['res.config.settings']
        for name in ('livechat_ai_base_url', 'livechat_ai_api_key', 'livechat_ai_api_key_set',
                     'livechat_ai_model_list', 'livechat_ai_log_payload',
                     'livechat_ai_log_retention_days'):
            self.assertEqual(Settings._fields[name].groups, 'base.group_system', name)

    def test_settings_buttons_refuse_non_admin(self):
        settings = self.env['res.config.settings'].create({})
        for user in (self.manager, self.lc_user):
            with self.assertRaises(AccessError):
                settings.with_user(user).action_livechat_ai_fetch_models()
            with self.assertRaises(AccessError):
                settings.with_user(user).action_livechat_ai_test_connection()

    def test_manager_read_channel_does_not_leak_key(self):
        for user in (self.manager, self.lc_user):
            data = self.channel.with_user(user).read()
            self.assertNotIn(SECRET_KEY, json.dumps(data, default=str))
            data = self.channel.with_user(user).web_read({'ai_model': {}, 'ai_enabled': {}})
            self.assertNotIn(SECRET_KEY, json.dumps(data, default=str))

    def test_non_admin_cannot_write_legacy_fields(self):
        for user in (self.manager, self.lc_user):
            with self.assertRaises(ValueError), mute_logger('odoo.models'):
                self.channel.with_user(user).write({'ai_api_base_url': 'https://attacker.example/v1'})
            with self.assertRaises(ValueError), mute_logger('odoo.models'):
                self.channel.with_user(user).write({'ai_api_key': 'sk-attacker'})

    def test_manager_can_enable_ai_and_choose_model(self):
        """The constraint runs as the manager; it probes the system key via sudo."""
        channel = self.env['im_livechat.channel'].with_user(self.manager).create({'name': 'Mgr Channel'})
        channel.write({'ai_enabled': True, 'ai_model': DEFAULT_MODEL})
        self.assertTrue(channel.ai_enabled)

    def test_manager_enable_without_system_key_fails_without_leak(self):
        self.env['ir.config_parameter'].set_param(ICP_API_KEY, False)
        channel = self.env['im_livechat.channel'].with_user(self.manager).create({'name': 'Mgr Channel 2'})
        with self.assertRaises(UserError) as ctx:
            channel.write({'ai_enabled': True, 'ai_model': DEFAULT_MODEL})
        self.assertIn('LiteLLM', str(ctx.exception))
        self.assertNotIn(TEST_BASE_URL, str(ctx.exception))

    @patch(POST)
    def test_manager_test_connection_uses_system_gateway(self, mock_post):
        """A manager can test, but the key only goes to the admin-set URL."""
        mock_post.return_value = _ok_chat_response()
        result = self.channel.with_user(self.manager).action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(mock_post.call_args[0][0], TEST_BASE_URL + '/chat/completions')
        self.assertEqual(mock_post.call_args[1]['headers']['Authorization'], f'Bearer {SECRET_KEY}')
        self.assertNotIn(SECRET_KEY, json.dumps(result, default=str))

    @patch(POST)
    def test_key_goes_only_to_system_url_even_with_context(self, mock_post):
        """Nothing a channel user controls changes the target URL."""
        mock_post.return_value = _ok_chat_response()
        channel = self.channel.with_user(self.lc_user).with_context(
            im_livechat_ai_base_url='https://attacker.example/v1',
        )
        channel.write({'ai_system_prompt': 'base url: https://attacker.example/v1'})
        channel._call_llm_api([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(mock_post.call_args[0][0], TEST_BASE_URL + '/chat/completions')

    @patch(POST)
    def test_call_llm_api_without_key_raises_without_http(self, mock_post):
        self.env['ir.config_parameter'].set_param(ICP_API_KEY, False)
        with self.assertRaises(LLMNotConfigured):
            self.channel._call_llm_api([{'role': 'user', 'content': 'hi'}])
        mock_post.assert_not_called()

    @patch(POST)
    def test_call_llm_api_rejects_non_http_base_url(self, mock_post):
        self.env['ir.config_parameter'].set_param(ICP_BASE_URL, 'file:///etc/passwd')
        with self.assertRaises(LLMNotConfigured):
            self.channel._call_llm_api([{'role': 'user', 'content': 'hi'}])
        mock_post.assert_not_called()

    def test_default_base_url_when_unset(self):
        self.env['ir.config_parameter'].set_param(ICP_BASE_URL, False)
        base_url, api_key = self.env['im_livechat.channel']._get_llm_connection()
        self.assertEqual(base_url, DEFAULT_BASE_URL)
        self.assertEqual(api_key, SECRET_KEY)

    def test_api_log_access(self):
        log = self.env['llm.api.log'].create({'status': 'success', 'model': DEFAULT_MODEL})
        # Live Chat user: no access at all.
        with self.assertRaises(AccessError):
            self.env['llm.api.log'].with_user(self.lc_user).search([])
        with self.assertRaises(AccessError):
            log.with_user(self.lc_user).read(['model'])
        # Live Chat manager: read (and delete) only.
        self.assertEqual(log.with_user(self.manager).read(['model'])[0]['model'], DEFAULT_MODEL)
        with self.assertRaises(AccessError):
            self.env['llm.api.log'].with_user(self.manager).create({'status': 'success'})
        with self.assertRaises(AccessError):
            log.with_user(self.manager).write({'model': 'forged'})

    def test_api_log_menu_and_actions_restricted(self):
        manager_group = self.env.ref('im_livechat.im_livechat_group_manager')
        system_group = self.env.ref('base.group_system')
        menu = self.env.ref('im_livechat_ai.llm_api_log_menu')
        action = self.env.ref('im_livechat_ai.llm_api_log_action')
        self.assertEqual(menu.groups_id, manager_group)
        self.assertEqual(action.groups_id, manager_group)
        settings_menu = self.env.ref('im_livechat_ai.livechat_ai_settings_menu')
        self.assertEqual(settings_menu.groups_id, system_group)
        self.assertFalse(self.env.ref('im_livechat_ai.access_llm_api_log_user', raise_if_not_found=False))
        user_menus = self.env['ir.ui.menu'].with_user(self.lc_user)._visible_menu_ids()
        self.assertNotIn(menu.id, user_menus)
        self.assertNotIn(settings_menu.id, user_menus)
        manager_menus = self.env['ir.ui.menu'].with_user(self.manager)._visible_menu_ids()
        self.assertNotIn(settings_menu.id, manager_menus)


class TestLiteLLMSettings(LivechatAiUsersCase):
    """Settings > Live Chat AI as a system administrator."""

    def _save_settings(self, vals, user=None):
        Settings = self.env['res.config.settings'].with_user(user or self.admin)
        settings = Settings.create(vals)
        settings.execute()
        return settings

    def test_admin_saves_connection(self):
        self._save_settings({
            'livechat_ai_base_url': '  https://gateway.example/v1  ',
            'livechat_ai_api_key': '  sk-new-admin-key  ',
        })
        ICP = self.env['ir.config_parameter']
        self.assertEqual(ICP.get_param(ICP_BASE_URL), 'https://gateway.example/v1')
        self.assertEqual(ICP.get_param(ICP_API_KEY), 'sk-new-admin-key')

    def test_api_key_is_write_only(self):
        settings = self._save_settings({'livechat_ai_api_key': 'sk-new-admin-key'})
        fresh = self.env['res.config.settings'].with_user(self.admin).create({})
        self.assertFalse(fresh.livechat_ai_api_key)
        self.assertTrue(fresh.livechat_ai_api_key_set)
        self.assertNotIn('sk-new-admin-key', json.dumps(fresh.read(), default=str))
        self.assertNotIn('sk-new-admin-key', json.dumps(settings.read(), default=str))
        # Not stored in the transient table either.
        self.assertFalse(sql.column_exists(self.env.cr, 'res_config_settings', 'livechat_ai_api_key'))

    def test_empty_key_keeps_existing_key(self):
        self._save_settings({'livechat_ai_api_key': False, 'livechat_ai_base_url': TEST_BASE_URL})
        self.assertEqual(self.env['ir.config_parameter'].get_param(ICP_API_KEY), SECRET_KEY)

    def test_defaults(self):
        ICP = self.env['ir.config_parameter']
        ICP.set_param(ICP_BASE_URL, False)
        ICP.set_param(ICP_LOG_RETENTION_DAYS, False)
        ICP.set_param(ICP_LOG_PAYLOAD, False)
        settings = self.env['res.config.settings'].with_user(self.admin).create({})
        self.assertEqual(settings.livechat_ai_base_url, DEFAULT_BASE_URL)
        self.assertEqual(settings.livechat_ai_log_retention_days, 14)
        self.assertFalse(settings.livechat_ai_log_payload)

    def test_invalid_base_url_rejected(self):
        with self.assertRaises(ValidationError):
            self._save_settings({'livechat_ai_base_url': 'ftp://gateway.example/v1'})

    def test_invalid_retention_rejected(self):
        with self.assertRaises(ValidationError):
            self._save_settings({'livechat_ai_log_retention_days': 0})

    def test_invalid_stored_retention_does_not_block_settings(self):
        self.env['ir.config_parameter'].set_param(ICP_LOG_RETENTION_DAYS, 'abc')
        with mute_logger('odoo.addons.base.models.res_config'):
            settings = self._save_settings({})
        self.assertEqual(settings.livechat_ai_log_retention_days, 14)

    @patch(GET)
    def test_fetch_models_caches_list(self, mock_get):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {'object': 'list', 'data': [
            {'id': 'chatgpt-pool/gpt-6-sol', 'object': 'model'},
            {'id': 'claude-pool/sonnet', 'object': 'model'},
            {'id': 'chatgpt-pool/gpt-6-sol', 'object': 'model'},
            {'object': 'model'},
        ]}
        response.raise_for_status = MagicMock()
        mock_get.return_value = response

        settings = self.env['res.config.settings'].with_user(self.admin).create({})
        result = settings.action_livechat_ai_fetch_models()

        self.assertEqual(result['params']['type'], 'success')
        self.assertIn('2', result['params']['message'])
        self.assertEqual(mock_get.call_args[0][0], TEST_BASE_URL + '/models')
        self.assertEqual(mock_get.call_args[1]['headers']['Authorization'], f'Bearer {SECRET_KEY}')
        cached = json.loads(self.env['ir.config_parameter'].get_param(ICP_MODEL_CACHE))
        self.assertEqual(cached, ['chatgpt-pool/gpt-6-sol', 'claude-pool/sonnet'])
        self.assertIn('claude-pool/sonnet', settings.livechat_ai_model_list)

        values = [v for v, _label in self.env['im_livechat.channel']._selection_ai_model()]
        self.assertEqual(values[0], DEFAULT_MODEL)
        self.assertIn('claude-pool/sonnet', values)

    @patch(GET)
    def test_fetch_models_failure_short_message(self, mock_get):
        mock_get.return_value = _http_error_response(401, '{"error": "upstream-secret-detail"}')
        self.env['ir.config_parameter'].set_param(ICP_MODEL_CACHE, json.dumps(['kept/model']))
        settings = self.env['res.config.settings'].with_user(self.admin).create({})
        result = settings.action_livechat_ai_fetch_models()
        msg = result['params']['message']
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('HTTP 401', msg)
        self.assertNotIn('upstream-secret-detail', msg)
        self.assertNotIn(SECRET_KEY, msg)
        # The previous cache is kept on failure.
        self.assertEqual(self.env['im_livechat.channel']._get_cached_llm_models(), ['kept/model'])

    @patch(GET)
    def test_fetch_models_invalid_payload(self, mock_get):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {'unexpected': True}
        response.raise_for_status = MagicMock()
        mock_get.return_value = response
        settings = self.env['res.config.settings'].with_user(self.admin).create({})
        result = settings.action_livechat_ai_fetch_models()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('回應格式不正確', result['params']['message'])

    def test_selection_keeps_values_in_use(self):
        """Existing channel values stay selectable even if not in the cache."""
        self.env['ir.config_parameter'].set_param(ICP_MODEL_CACHE, json.dumps(['a/model']))
        self.env.cr.execute(SQL(
            "UPDATE im_livechat_channel SET ai_model = %s WHERE id = %s", 'MiniMax-M1', self.channel.id,
        ))
        self.channel.invalidate_recordset()
        values = [v for v, _label in self.env['im_livechat.channel']._selection_ai_model()]
        self.assertIn('MiniMax-M1', values)
        self.assertIn('a/model', values)
        self.assertIn(DEFAULT_MODEL, values)
        self.assertEqual(self.channel.ai_model, 'MiniMax-M1')
        # The manager's form can still read it.
        self.assertEqual(self.channel.with_user(self.manager).read(['ai_model'])[0]['ai_model'], 'MiniMax-M1')

    def test_selection_ignores_corrupt_cache(self):
        self.env['ir.config_parameter'].set_param(ICP_MODEL_CACHE, '{not json')
        with mute_logger('odoo.addons.im_livechat_ai.models.im_livechat_channel'):
            values = [v for v, _label in self.env['im_livechat.channel']._selection_ai_model()]
        self.assertIn(DEFAULT_MODEL, values)

    @patch(POST)
    def test_settings_test_connection(self, mock_post):
        settings = self.env['res.config.settings'].with_user(self.admin).create({})
        mock_post.return_value = _ok_chat_response()
        result = settings.action_livechat_ai_test_connection()
        self.assertEqual(result['params']['type'], 'success')
        self.assertIn('HTTP 200', result['params']['message'])
        self.assertEqual(mock_post.call_args[1]['json']['model'], DEFAULT_MODEL)

        mock_post.return_value = _http_error_response(429, 'quota exceeded for team xyz')
        result = settings.action_livechat_ai_test_connection()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertIn('HTTP 429', result['params']['message'])
        self.assertNotIn('team xyz', result['params']['message'])

        mock_post.return_value = _http_error_response(502, '<html>bad gateway</html>')
        result = settings.action_livechat_ai_test_connection()
        self.assertIn('HTTP 502', result['params']['message'])
        self.assertNotIn('<html>', result['params']['message'])

    def test_turning_payload_logging_off_erases_content(self):
        self._save_settings({'livechat_ai_log_payload': True})
        log = self.env['llm.api.log'].create({
            'status': 'success',
            'request_payload': '[{"role": "user", "content": "我的電話是 0912"}]',
            'response_payload': '{"choices": []}',
        })
        self._save_settings({'livechat_ai_log_payload': False})
        self.assertFalse(log.request_payload)
        self.assertFalse(log.response_payload)
        self.assertFalse(self.env['ir.config_parameter'].get_param(ICP_LOG_PAYLOAD))


class TestApiLogPrivacy(LivechatAiUsersCase):
    """Package F: conversation content only with the toggle on; retention."""

    def _last_log(self, status='success'):
        return self.env['llm.api.log'].search([
            ('livechat_channel_id', '=', self.channel.id), ('status', '=', status),
        ], order='id desc', limit=1)

    @patch(POST)
    def test_payload_not_logged_by_default(self, mock_post):
        self._add_visitor_msg()
        mock_post.return_value = _ok_chat_response('價格是 NT$1,520')
        self.channel._process_ai_response(self.env.cr.dbname, self.channel.id, self.session.id,
                                          test_env=self.env)
        log = self._last_log()
        self.assertTrue(log)
        self.assertFalse(log.request_payload)
        self.assertFalse(log.response_payload)
        # Metadata is still recorded.
        self.assertEqual(log.model, DEFAULT_MODEL)
        self.assertEqual(log.total_tokens, 5)
        self.assertTrue(log.timestamp)
        self.assertIsNotNone(log.response_time)

    @patch(POST)
    def test_payload_logged_when_enabled(self, mock_post):
        self.env['ir.config_parameter'].set_param(ICP_LOG_PAYLOAD, True)
        self._add_visitor_msg('請問保固多久')
        mock_post.return_value = _ok_chat_response('保固一年')
        self.channel._process_ai_response(self.env.cr.dbname, self.channel.id, self.session.id,
                                          test_env=self.env)
        log = self._last_log()
        self.assertIn('請問保固多久', log.request_payload)
        self.assertIn('保固一年', log.response_payload)

    @patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep')
    @patch(POST)
    def test_error_log_has_no_payload_and_no_body(self, mock_post, mock_sleep):
        self._add_visitor_msg('我的地址是台北市')
        mock_post.return_value = _http_error_response(500, 'stacktrace with internal hostnames')
        self.channel._process_ai_response(self.env.cr.dbname, self.channel.id, self.session.id,
                                          test_env=self.env)
        logs = self.env['llm.api.log'].search([('livechat_channel_id', '=', self.channel.id)])
        self.assertTrue(logs)
        for log in logs:
            self.assertFalse(log.request_payload)
            self.assertFalse(log.response_payload)
            self.assertNotIn('internal hostnames', log.error_message or '')
            self.assertNotIn(SECRET_KEY, log.error_message or '')
        self.assertIn('HTTP 500', self._last_log('error').error_message)

    def test_retention_setting(self):
        ICP = self.env['ir.config_parameter']
        Log = self.env['llm.api.log']
        log_20 = Log.create({'status': 'success', 'timestamp': fields.Datetime.now() - timedelta(days=20)})
        log_40 = Log.create({'status': 'success', 'timestamp': fields.Datetime.now() - timedelta(days=40)})
        ICP.set_param(ICP_LOG_RETENTION_DAYS, '30')
        self.assertEqual(Log._cleanup_old_logs(), 1)
        self.assertTrue(log_20.exists())
        self.assertFalse(log_40.exists())

    def test_retention_default_and_invalid(self):
        ICP = self.env['ir.config_parameter']
        Log = self.env['llm.api.log']
        ICP.set_param(ICP_LOG_RETENTION_DAYS, False)
        self.assertEqual(Log._get_retention_days(), 14)
        ICP.set_param(ICP_LOG_RETENTION_DAYS, 'abc')
        with mute_logger('odoo.addons.im_livechat_ai.models.llm_api_log'):
            self.assertEqual(Log._get_retention_days(), 14)
        ICP.set_param(ICP_LOG_RETENTION_DAYS, '-3')
        self.assertEqual(Log._get_retention_days(), 1)

    def test_cron_uses_cleanup(self):
        cron = self.env.ref('im_livechat_ai.ir_cron_cleanup_llm_api_logs')
        self.assertIn('_cleanup_old_logs', cron.code)
        old = self.env['llm.api.log'].create({
            'status': 'success', 'timestamp': fields.Datetime.now() - timedelta(days=15),
        })
        cron.method_direct_trigger()
        self.assertFalse(old.exists())


def _load_migration(name):
    path = f"{get_module_path('im_livechat_ai')}/migrations/18.0.1.1.0/{name}.py"
    spec = importlib.util.spec_from_file_location(f'im_livechat_ai_mig_{name.replace("-", "_")}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration18011(TransactionCase):
    """Upgrade from 18.0.1.0.x: legacy credentials wiped, all channels disabled."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.pre = _load_migration('pre-migrate')
        cls.post = _load_migration('post-migrate')
        Channel = cls.env['im_livechat.channel']
        cls.ch_on = Channel.create({'name': 'Legacy On', 'ai_enabled': True, 'ai_model': 'MiniMax-M1'})
        cls.ch_off = Channel.create({'name': 'Legacy Off', 'ai_enabled': False})

    def _add_legacy_columns(self):
        cr = self.env.cr
        for column in ('ai_api_key', 'ai_api_base_url'):
            if not sql.column_exists(cr, 'im_livechat_channel', column):
                cr.execute(SQL("ALTER TABLE im_livechat_channel ADD COLUMN %s varchar", SQL.identifier(column)))
        cr.execute("UPDATE im_livechat_channel SET ai_api_key = 'sk-leaked-minimax', "
                   "ai_api_base_url = 'https://api.minimax.example/v1'")

    def test_fresh_install_is_noop(self):
        self._add_legacy_columns()
        self.pre.migrate(self.env.cr, None)
        self.post.migrate(self.env.cr, None)
        self.env.cr.execute("SELECT count(*) FROM im_livechat_channel WHERE ai_api_key IS NOT NULL")
        self.assertGreater(self.env.cr.fetchone()[0], 0)

    def test_pre_migrate_wipes_credentials(self):
        self._add_legacy_columns()
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        self.env.cr.execute("""SELECT count(*) FROM im_livechat_channel
                                WHERE ai_api_key IS NOT NULL OR ai_api_base_url IS NOT NULL""")
        self.assertEqual(self.env.cr.fetchone()[0], 0)

    def test_post_migrate_drops_columns_and_disables_ai(self):
        self._add_legacy_columns()
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        logger_name = self.post.__name__
        with self.assertLogs(logger_name, level='WARNING') as logs:
            self.post.migrate(self.env.cr, '18.0.1.0.1')
        self.assertTrue(any('must set the LiteLLM' in line for line in logs.output))
        self.assertTrue(any(str(self.ch_on.id) in line for line in logs.output))
        for column in ('ai_api_key', 'ai_api_base_url'):
            self.assertFalse(sql.column_exists(self.env.cr, 'im_livechat_channel', column))
        self.env.cr.execute("SELECT count(*) FROM im_livechat_channel WHERE ai_enabled")
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.env.invalidate_all()
        self.assertFalse(self.ch_on.ai_enabled)
        # Existing model value is kept and still selectable.
        self.assertEqual(self.ch_on.ai_model, 'MiniMax-M1')

    def test_post_migrate_scrubs_logs(self):
        Log = self.env['llm.api.log']
        dirty = Log.create({
            'status': 'error',
            'request_payload': '[{"role": "user", "content": "電話 0912"}]',
            'response_payload': '{"x": 1}',
            'error_message': 'All 3 retries exhausted. Last error: 401 | Response: {"secret": "body"}',
        })
        long_error = Log.create({'status': 'error', 'error_message': 'E' * 2000})
        with self.assertLogs(self.post.__name__, level='INFO'):
            self.post.migrate(self.env.cr, '18.0.1.0.1')
        self.env.invalidate_all()
        self.assertFalse(dirty.request_payload)
        self.assertFalse(dirty.response_payload)
        self.assertEqual(dirty.error_message, 'All 3 retries exhausted. Last error: 401')
        self.assertEqual(len(long_error.error_message), 300)

    def test_migration_idempotent_without_legacy_columns(self):
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        with self.assertLogs(self.post.__name__, level='WARNING'):
            self.post.migrate(self.env.cr, '18.0.1.0.1')
        self.env.invalidate_all()
        self.assertFalse(self.ch_on.ai_enabled)
