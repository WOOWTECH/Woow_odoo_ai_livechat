# -*- coding: utf-8 -*-
"""AI assistants (「AI 助理」), the channel's pick and the "in use" guard (18.0.2.0.0)."""

import base64

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger

from .common import TEST_BACKEND, make_ai_channel, make_assistant, make_connection

LOGGER = 'odoo.addons.im_livechat_ai.models.ai_connection'


class TestAssistant(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = make_connection(cls.env, name='LiteLLM')
        cls.category = cls.env.ref('im_livechat_ai.partner_category_ai_bot')

    def test_bot_partner_on_create(self):
        assistant = make_assistant(self.env, self.connection, name='小沃')
        partner = assistant.partner_id
        self.assertTrue(partner)
        self.assertEqual(partner.name, '小沃')
        self.assertFalse(partner.active)
        self.assertIn(self.category, partner.category_id)
        self.assertTrue(base64.b64decode(partner.image_1920).startswith(b'\x89PNG'))
        self.assertFalse(partner.user_ids)

    def test_sync_name_and_image_never_activates(self):
        assistant = make_assistant(self.env, self.connection, name='A')
        partner = assistant.partner_id
        png = self.env['im_livechat.channel']._ai_bot_avatar()
        assistant.write({'name': 'B', 'image_1920': png})
        self.assertEqual(partner.name, 'B')
        self.assertFalse(partner.active)
        assistant.image_1920 = False
        self.assertTrue(partner.image_1920, 'clearing the image gives the bot the robot avatar')

    def test_copy(self):
        assistant = make_assistant(self.env, self.connection, name='A')
        copy = assistant.copy()
        self.assertNotEqual(copy.partner_id, assistant.partner_id)
        self.assertEqual(copy.name, 'A (copy)')
        self.assertFalse(copy.livechat_available, 'the tick is not copied')

    def test_duplicate_names_show_the_id(self):
        first = make_assistant(self.env, self.connection, name='同名')
        second = make_assistant(self.env, self.connection, name='同名')
        alone = make_assistant(self.env, self.connection, name='獨一')
        self.assertEqual(first.display_name, '同名 (#%s)' % first.id)
        self.assertEqual(second.display_name, '同名 (#%s)' % second.id)
        self.assertEqual(alone.display_name, '獨一')
        second.active = False
        first.invalidate_recordset(['display_name'])
        self.assertEqual(first.display_name, '同名')
        # visitors only see the partner's name
        self.assertEqual(first.partner_id.name, '同名')

    def test_tick_and_selectable(self):
        assistant = self.env['im_livechat.ai.assistant'].create({'name': 'New', 'connection_id': self.connection.id})
        self.assertFalse(assistant.livechat_available, 'False by default on new assistants')
        self.assertFalse(assistant.livechat_selectable)
        assistant.livechat_available = True
        self.assertTrue(assistant.livechat_selectable)
        Assistant = self.env['im_livechat.ai.assistant']
        self.assertIn(assistant, Assistant.search([('livechat_selectable', '=', True)]))
        assistant.active = False
        self.assertNotIn(assistant, Assistant.with_context(active_test=False).search(
            [('livechat_selectable', '=', True)]))
        with self.assertRaises(UserError):
            Assistant.search([('livechat_selectable', 'in', [True])])

    def test_customer_safe_follows_the_connection(self):
        n8n = make_connection(self.env, name='n8n', backend_type='n8n', endpoint_url='https://n8n.example/hook')
        assistant = make_assistant(self.env, n8n)
        self.assertFalse(assistant.customer_safe)
        n8n.customer_safe = True
        assistant.invalidate_recordset(['customer_safe'])
        self.assertTrue(assistant.customer_safe)
        self.assertIn(assistant, self.env['im_livechat.ai.assistant'].search([('customer_safe', '=', True)]))

    def test_job_runner_info(self):
        assistant = make_assistant(self.env, self.connection)
        self.assertIn('Live Chat AI: Recover Reply Jobs', assistant.job_runner_info)


class TestChannelPick(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='pick_manager', name='Manager',
                                    groups='base.group_user,im_livechat.im_livechat_group_manager')
        cls.connection = make_connection(cls.env, name='LiteLLM')
        cls.assistant = make_assistant(cls.env, cls.connection, name='官網助理', description='回答商品問題')
        cls.channel = cls.env['im_livechat.channel'].create({'name': 'Pick'})

    def test_manager_picks_an_available_assistant(self):
        channel = self.channel.with_user(self.manager)
        channel.write({'ai_assistant_id': self.assistant.id, 'ai_enabled': True})
        self.assertEqual(channel.ai_state, 'ok')
        self.assertEqual(channel.ai_assistant_description, '回答商品問題')
        domain = channel._fields['ai_assistant_id'].domain
        options = self.env['im_livechat.ai.assistant'].with_user(self.manager).search(domain)
        self.assertIn(self.assistant, options)

    def test_archived_or_unticked_is_refused(self):
        archived = make_assistant(self.env, self.connection, name='舊的')
        archived.active = False
        with self.assertRaises(ValidationError):
            self.channel.with_user(self.manager).write({'ai_assistant_id': archived.id})
        unticked = make_assistant(self.env, self.connection, name='內部用', livechat_available=False)
        with self.assertRaises(ValidationError):
            self.channel.with_user(self.manager).write({'ai_assistant_id': unticked.id})
        domain = self.channel._fields['ai_assistant_id'].domain
        options = self.env['im_livechat.ai.assistant'].with_user(self.manager).search(domain)
        self.assertNotIn(unticked, options)

    def test_enable_needs_a_ready_assistant(self):
        with self.assertRaises(UserError) as caught:
            self.channel.with_user(self.manager).write({'ai_enabled': True})
        self.assertIn('has no AI assistant that can answer yet', str(caught.exception))
        blocked = make_assistant(self.env, make_connection(self.env, name='no key', endpoint_url='https://x.example/v1',
                                                           model='m'))
        self.channel.write({'ai_assistant_id': blocked.id})
        with self.assertRaises(UserError):
            self.channel.with_user(self.manager).write({'ai_enabled': True})

    def test_state_reason_has_no_secret(self):
        connection = make_connection(self.env, name='Partial', endpoint_url='https://secret-host.example/v1',
                                     api_key='dummy-secret-key')
        assistant = make_assistant(self.env, connection)
        self.channel.write({'ai_assistant_id': assistant.id})
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = TRUE WHERE id = %s", (self.channel.id,))
        self.channel.invalidate_recordset()
        channel = self.channel.with_user(self.manager)
        self.assertEqual(channel.ai_state, 'blocked')
        self.assertIn('Set the model', channel.ai_state_reason)
        for secret in ('secret-host', 'dummy-secret-key', 'Partial'):
            self.assertNotIn(secret, channel.ai_state_reason)
        channel.write({'ai_enabled': False})
        self.assertEqual(channel.ai_state, 'off')

    def test_customer_safe_warning_for_n8n(self):
        n8n = make_connection(self.env, name='n8n', backend_type='n8n', endpoint_url='https://n8n.example/hook')
        self.channel.write({'ai_assistant_id': make_assistant(self.env, n8n).id})
        self.assertTrue(self.channel.with_user(self.manager).ai_customer_safe_warning)
        self.channel.write({'ai_assistant_id': self.assistant.id})
        self.assertFalse(self.channel.with_user(self.manager).ai_customer_safe_warning)

    def test_copy_keeps_active_drops_archived(self):
        self.channel.write({'ai_assistant_id': self.assistant.id})
        self.assertEqual(self.channel.copy().ai_assistant_id, self.assistant)
        self.env.cr.execute("UPDATE im_livechat_ai_assistant SET active = FALSE WHERE id = %s", (self.assistant.id,))
        self.assistant.invalidate_recordset()
        self.assertFalse(self.channel.copy().ai_assistant_id)


class TestInUseGuard(TransactionCase):
    """18.0.1.x refused saving an AI-enabled channel without a complete connection;
    18.0.2.0.0 keeps that rule on the connection and the assistant."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = make_ai_channel(cls.env, {**TEST_BACKEND, 'name': '使用中', 'ai_enabled': True})
        cls.assistant = cls.channel.sudo().ai_assistant_id
        cls.connection = cls.assistant.connection_id

    def assertRefused(self, function):
        with self.assertRaises(UserError) as caught, self.env.cr.savepoint(), mute_logger(LOGGER):
            function()
        self.assertIn('使用中', str(caught.exception))

    def test_refused_while_ai_enabled(self):
        self.assertRefused(lambda: self.assistant.write({'active': False}))
        self.assertRefused(lambda: self.assistant.write({'livechat_available': False}))
        blocked = make_connection(self.env, name='blocked', endpoint_url='https://x.example/v1')
        self.assertRefused(lambda: self.assistant.write({'connection_id': blocked.id}))
        self.assertRefused(lambda: self.connection.write({'active': False}))
        self.assertRefused(lambda: self.connection.action_clear_api_key())
        self.assertRefused(lambda: self.connection.write({'model': False}))
        self.assertRefused(lambda: self.connection.write({'endpoint_url': False}))
        # another host without a new key drops the key
        self.assertRefused(lambda: self.connection.write({'endpoint_url': 'https://other.example/v1'}))
        self.assertTrue(self.connection.api_key_set)
        self.assertTrue(self.assistant.active)
        # with a new key the same change passes
        self.connection.write({'endpoint_url': 'https://other.example/v1', 'api_key': 'dummy-new'})

    def test_allowed_once_the_ai_is_off(self):
        self.channel.write({'ai_enabled': False})
        with mute_logger(LOGGER):
            self.connection.write({'endpoint_url': 'https://other.example/v1'})
        self.connection.action_clear_api_key()
        self.assistant.write({'livechat_available': False})
        self.assistant.write({'active': False})
        self.connection.write({'active': False})
