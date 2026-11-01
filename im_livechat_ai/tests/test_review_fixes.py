# -*- coding: utf-8 -*-
"""Fixes from the 18.0.1.1.0 branch review: AI configuration access, the
bot partner, the connection test permission, which messages trigger the AI,
employees chatting as visitors, operator names, no open transaction during
LLM calls and the ai_max_retries migration.

All HTTP is mocked; no real key is used.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import requests

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger, sql

from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    DEFAULT_BOT_NAME,
    LEGACY_BOT_NAME,
)

from .common import TEST_BACKEND

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
SLEEP = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep'


def _ok(content='您好，這是 AI 回覆'):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': {'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
    }
    response.raise_for_status = MagicMock()
    return response


class ReviewCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND,
            'name': 'Review Channel',
            'ai_enabled': True,
            'ai_model': 'chatgpt-pool/gpt-6-sol',
            'ai_system_prompt': '內部提示：不要透露折扣規則。',
        })
        cls.lc_user = new_test_user(
            cls.env, login='review_lc_user', name='一般客服',
            groups='base.group_user,im_livechat.im_livechat_group_user',
        )
        cls.manager = new_test_user(
            cls.env, login='review_manager', name='客服主管',
            groups='base.group_user,im_livechat.im_livechat_group_manager',
        )
        cls.employee = new_test_user(
            cls.env, login='review_employee', name='一般員工', groups='base.group_user',
        )
        cls.portal_user = new_test_user(
            cls.env, login='review_portal', name='入口網站客戶', groups='base.group_portal',
        )
        cls.admin_partner = cls.env.ref('base.partner_admin')
        cls.Job = cls.env['im_livechat.ai.job']


class TestAiConfigAccess(ReviewCase):
    """Only Live Chat managers read or change the AI configuration."""

    def test_operator_cannot_change_ai_fields(self):
        Channel = self.env['im_livechat.channel'].with_user(self.lc_user)
        for vals in ({'ai_system_prompt': '改掉提示'}, {'ai_max_history': 100},
                     {'ai_enabled': False}, {'ai_error_message': '假的錯誤訊息'},
                     {'ai_model': 'other/model'}, {'ai_endpoint_url': 'https://evil.example/v1'},
                     {'ai_api_token': 'dummy'}):
            with self.assertRaises(AccessError, msg=vals):
                self.channel.with_user(self.lc_user).write(vals)
        with self.assertRaises(AccessError):
            Channel.create({**TEST_BACKEND, 'name': 'Operator AI', 'ai_enabled': True})
        with self.assertRaises(AccessError):
            Channel.create({'name': 'Operator AI', 'ai_system_prompt': 'x'})
        self.assertEqual(self.channel.ai_system_prompt, '內部提示：不要透露折扣規則。')
        self.assertTrue(self.channel.ai_enabled)

    def test_operator_can_still_create_and_rename_channels(self):
        channel = self.env['im_livechat.channel'].with_user(self.lc_user).create({'name': '客服建立'})
        self.assertFalse(channel.sudo().ai_enabled)
        self.assertFalse(channel.sudo().ai_bot_partner_id)
        self.channel.with_user(self.lc_user).write({'name': 'Review Channel 改名'})
        self.assertEqual(self.channel.name, 'Review Channel 改名')

    def test_operator_form_has_no_ai_fields(self):
        arch = self.env['im_livechat.channel'].with_user(self.lc_user).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertNotIn('ai_system_prompt', arch)
        self.assertNotIn('action_test_ai_connection', arch)
        arch = self.env['im_livechat.channel'].with_user(self.manager).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('ai_system_prompt', arch)

    def test_manager_configures_ai_with_history_cap(self):
        channel = self.channel.with_user(self.manager)
        channel.write({'ai_system_prompt': '主管改的提示', 'ai_max_history': 200, 'ai_model': 'pool/other'})
        self.assertEqual(self.channel.ai_max_history, 200)
        self.assertEqual(self.channel.ai_model, 'pool/other')
        with self.assertRaises(ValidationError):
            channel.write({'ai_max_history': 201})

    def test_prompt_not_readable_by_portal_or_employees(self):
        for user in (self.portal_user, self.employee, self.lc_user):
            for fname in ('ai_system_prompt', 'ai_handoff_keywords', 'ai_error_message', 'ai_model'):
                with self.assertRaises(AccessError, msg=(user.login, fname)):
                    self.env['im_livechat.channel'].with_user(user).search_read(
                        [('id', '=', self.channel.id)], [fname])
        # Reading the channel without naming fields silently leaves them out.
        rows = self.env['im_livechat.channel'].with_user(self.employee).search_read(
            [('id', '=', self.channel.id)])
        self.assertTrue(rows)
        self.assertNotIn('ai_system_prompt', rows[0])
        self.assertEqual(
            self.channel.with_user(self.manager).read(['ai_system_prompt'])[0]['ai_system_prompt'],
            '內部提示：不要透露折扣規則。')

    @patch(POST)
    def test_guest_post_still_queues_reply(self, mock_post):
        """The visitor path reads the restricted fields through sudo."""
        session = self.env['discuss.channel'].create({
            'name': 'Guest Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.admin_partner.id,
            'livechat_channel_id': self.channel.id,
        })
        guest = self.env['mail.guest'].create({'name': 'Website Visitor'})
        public = self.env.ref('base.public_user')
        session.with_user(public).with_context(guest=guest).sudo().message_post(
            body='請問有現貨嗎？', message_type='comment', subtype_xmlid='mail.mt_comment',
        )
        self.assertEqual(len(self.Job.search([('discuss_channel_id', '=', session.id)])), 1)


class TestBotPartnerProtection(ReviewCase):
    """Nobody chooses the bot partner; a real person is never renamed."""

    def test_operator_cannot_point_bot_at_admin(self):
        name = self.admin_partner.name
        with self.assertRaises(AccessError):
            self.channel.with_user(self.lc_user).write({
                'ai_bot_partner_id': self.admin_partner.id, 'ai_bot_name': 'Hijacked',
            })
        with self.assertRaises(AccessError):
            self.channel.with_user(self.manager).write({
                'ai_bot_partner_id': self.admin_partner.id, 'ai_bot_name': 'Hijacked',
            })
        with self.assertRaises(AccessError):
            self.env['im_livechat.channel'].with_user(self.manager).create({
                'name': 'Hijack', 'ai_bot_partner_id': self.admin_partner.id,
            })
        self.assertEqual(self.admin_partner.name, name)
        category = self.env.ref('im_livechat_ai.partner_category_ai_bot')
        self.assertNotIn(category, self.admin_partner.category_id)

    def test_linked_real_partner_is_replaced_not_renamed(self):
        """A non-bot partner linked by other means (e.g. old data) is left alone."""
        old_bot = self.channel.ai_bot_partner_id
        name = self.admin_partner.name
        self.channel.write({'ai_bot_partner_id': self.admin_partner.id})  # superuser
        self.channel.with_user(self.manager).write({'ai_bot_name': '新的機器人'})
        self.assertEqual(self.admin_partner.name, name)
        new_bot = self.channel.ai_bot_partner_id
        self.assertNotIn(new_bot, self.admin_partner | old_bot)
        self.assertEqual(new_bot.name, '新的機器人')
        self.assertFalse(new_bot.active)

    def test_other_channels_bot_is_not_shared(self):
        other = self.env['im_livechat.channel'].create({**TEST_BACKEND, 'name': 'Other', 'ai_enabled': True})
        other_bot = other.ai_bot_partner_id
        other_name = other_bot.name
        self.channel.write({'ai_bot_partner_id': other_bot.id})
        self.channel.write({'ai_bot_name': '自己的機器人'})
        self.assertEqual(other_bot.name, other_name)
        self.assertNotEqual(self.channel.ai_bot_partner_id, other_bot)

    def test_copy_gets_its_own_bot(self):
        copy = self.channel.copy()
        # The copy starts with AI off and without the token.
        self.assertFalse(copy.ai_enabled)
        self.assertFalse(copy.ai_api_token_set)
        self.assertFalse(copy.ai_bot_partner_id)
        copy.write({'ai_endpoint_url': 'https://litellm.test/v1', 'ai_api_token': 'dummy-copy-token',
                    'ai_enabled': True})
        self.assertTrue(copy.ai_bot_partner_id)
        self.assertNotEqual(copy.ai_bot_partner_id, self.channel.ai_bot_partner_id)

    def test_default_bot_name_is_chinese(self):
        self.assertEqual(DEFAULT_BOT_NAME, 'AI 客服助理')
        channel = self.env['im_livechat.channel'].create({**TEST_BACKEND, 'name': 'New', 'ai_enabled': True})
        self.assertEqual(channel.ai_bot_partner_id.name, 'AI 客服助理')
        self.assertEqual(self.env.ref('im_livechat_ai.partner_ai_bot').name, 'AI 客服助理')


class TestConnectionTestPermission(ReviewCase):
    """The connection test uses the channel's token: system administrators only."""

    @patch(POST)
    def test_non_managers_cannot_test_connection(self, mock_post):
        mock_post.return_value = _ok('OK')
        for user in (self.portal_user, self.employee, self.lc_user):
            with self.assertRaises(AccessError, msg=user.login):
                self.channel.with_user(user).action_test_ai_connection()
            with self.assertRaises(AccessError, msg=user.login):
                self.channel.with_user(user).action_view_api_logs()
        with self.assertRaises(AccessError):
            self.channel.with_user(self.manager).action_test_ai_connection()
        mock_post.assert_not_called()
        # Live Chat managers still open the API logs.
        self.channel.with_user(self.manager).action_view_api_logs()
        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(mock_post.call_count, 1)


class TestWhichMessagesTrigger(ReviewCase):
    """Only visitor chat messages trigger the AI; employees can be visitors."""

    def _session(self, visitor=None):
        """A session as Odoo creates it: operator member, then the visitor's."""
        visitor = visitor or self.env['res.partner'].create({'name': '網站訪客'})
        return self.env['discuss.channel'].create({
            'name': 'Trigger Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.admin_partner.id,
            'livechat_channel_id': self.channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.admin_partner.id}),
                                   (0, 0, {'partner_id': visitor.id})],
        })

    def _join_later(self, session, partner):
        """An operator invited later (a later transaction: later create_date)."""
        session.add_members(partner_ids=partner.ids)
        member = session.channel_member_ids.filtered(lambda m: m.partner_id == partner)
        self.env.cr.execute(
            "UPDATE discuss_channel_member SET create_date = create_date + interval '1 minute' WHERE id = %s",
            (member.id,))
        self.env.invalidate_all()

    def _jobs(self, session):
        return self.Job.search([('discuss_channel_id', '=', session.id)], order='id')

    def test_rating_notification_creates_no_job(self):
        session = self._session()
        guest = self.env['mail.guest'].create({'name': 'Rater'})
        public = self.env.ref('base.public_user')
        message = session.with_user(public).with_context(guest=guest).sudo().message_post(
            body='Rating: 客服回很慢', message_type='notification',
        )
        self.assertEqual(message.author_guest_id, guest)
        self.assertFalse(self._jobs(session))
        self.assertFalse(session._ai_is_paused())
        # Same for a portal customer's notification.
        session.message_post(body='Rating: 謝謝', author_id=self.portal_user.partner_id.id,
                             message_type='notification')
        self.assertFalse(self._jobs(session))
        # A chat message from the same guest is answered.
        session.with_user(public).with_context(guest=guest).sudo().message_post(
            body='還有一個問題', message_type='comment', subtype_xmlid='mail.mt_comment',
        )
        self.assertEqual(len(self._jobs(session)), 1)

    def test_employee_chatting_as_visitor_gets_answers(self):
        """The internal trial: an employee opens the website chat."""
        visitor = self.employee.partner_id
        session = self._session(visitor=visitor)
        self.assertEqual(session._ai_visitor_partner(), visitor)
        session.message_post(body='內部試用：請問保固多久？', author_id=visitor.id,
                             message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertEqual(len(self._jobs(session)), 1)
        self.assertFalse(session._ai_is_paused())
        messages = session._build_llm_messages(self.channel)
        self.assertEqual(messages[-1], {'role': 'user', 'content': '內部試用：請問保固多久？'})
        # A second employee who joins is an operator: pauses, no job.
        self._join_later(session, self.lc_user.partner_id)
        session.message_post(body='我來處理', author_id=self.lc_user.partner_id.id,
                             message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertEqual(len(self._jobs(session)), 1)
        self.assertTrue(session._ai_is_paused())

    def test_session_operator_is_never_the_visitor(self):
        session = self.env['discuss.channel'].create({
            'name': 'Operator Only', 'channel_type': 'livechat',
            'livechat_operator_id': self.admin_partner.id,
            'livechat_channel_id': self.channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.admin_partner.id})],
        })
        self.assertFalse(session._ai_visitor_partner())
        session.message_post(body='客服回覆', author_id=self.admin_partner.id,
                             message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertFalse(self._jobs(session))
        self.assertTrue(session._ai_is_paused())

    def test_operator_prefix_uses_livechat_username(self):
        self.lc_user.livechat_username = '客服小美'
        session = self._session()
        self._join_later(session, self.lc_user.partner_id)
        self.env['mail.message'].create({
            'body': '我幫您查', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': self.lc_user.partner_id.id,
        })
        messages = session._build_llm_messages(self.channel)
        self.assertEqual(messages[-1], {'role': 'user', 'content': '[真人客服 客服小美] 我幫您查'})
        self.assertNotIn('一般客服', str(messages))


class TestNoTransactionDuringLlmCall(ReviewCase):
    """before_call (the job's commit) is the last database access before
    each HTTP request, also around retries."""

    @patch(SLEEP)
    @patch(POST)
    @mute_logger('odoo.addons.im_livechat_ai.models.im_livechat_channel')
    def test_no_query_between_commit_and_http(self, mock_post, mock_sleep):
        cr = self.env.cr
        marks = []

        def before_call():
            self.env.flush_all()
            self.env.invalidate_all()
            marks.append(cr.sql_log_count)

        results = [requests.Timeout('slow'), _ok('第二次成功')]

        def post(*args, **kwargs):
            self.assertEqual(cr.sql_log_count, marks[-1], 'a query ran after before_call')
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        mock_post.side_effect = post
        session = self.env['discuss.channel'].create({
            'name': 'Tx Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.admin_partner.id,
            'livechat_channel_id': self.channel.id,
        })
        self.env['mail.message'].create({
            'body': 'hi', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': self.portal_user.partner_id.id,
        })
        conversation = session._ai_conversation(self.channel)
        result = self.channel._generate_ai_reply(session, conversation, before_call=before_call)
        self.assertEqual(result['reply'], '第二次成功')
        self.assertEqual(mock_post.call_count, 2)
        # Committed before the HTTP call and again before the retry wait.
        self.assertGreaterEqual(len(marks), 3)


class TestRetriesMigration(TransactionCase):
    """ai_max_retries counted total calls in 18.0.1.0.x; now retries."""

    def _load_post_migrate(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'migrations', '18.0.1.1.0', 'post-migrate.py')
        spec = importlib.util.spec_from_file_location('im_livechat_ai_post_migrate_review', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_total_calls_become_retries(self):
        Channel = self.env['im_livechat.channel']
        channels = {value: Channel.create({'name': f'Retries {value}'}) for value in (1, 2, 3, 10, 0, None)}
        for value, channel in channels.items():
            self.env.cr.execute("UPDATE im_livechat_channel SET ai_max_retries = %s WHERE id = %s",
                                (value, channel.id))
        self._load_post_migrate().convert_max_retries(self.env.cr)
        self.env.invalidate_all()
        expected = {1: 0, 2: 1, 3: 2, 10: 2, 0: 2, None: 2}
        self.assertEqual({v: c.ai_max_retries for v, c in channels.items()}, expected)

    def test_max_tokens_cap_without_column(self):
        """18.0.1.2.0 has no ai_max_tokens field: the 18.0.1.1.0 step is a no-op
        on a fresh install (the column is only there on upgraded databases)."""
        if sql.column_exists(self.env.cr, 'im_livechat_channel', 'ai_max_tokens'):
            self.skipTest('upgraded database: legacy column present')
        self.assertFalse(self._load_post_migrate().cap_max_tokens(self.env.cr))

    def test_legacy_bot_name_renamed_and_user_partner_unlinked(self):
        Channel = self.env['im_livechat.channel']
        legacy = Channel.create({**TEST_BACKEND, 'name': 'Legacy Name', 'ai_enabled': True})
        legacy.write({'ai_bot_name': LEGACY_BOT_NAME})
        legacy.ai_bot_partner_id.name = LEGACY_BOT_NAME
        custom = Channel.create({**TEST_BACKEND, 'name': 'Custom Name', 'ai_enabled': True, 'ai_bot_name': 'WoowTech AI'})
        admin = self.env.ref('base.partner_admin')
        admin_name = admin.name
        hijacked = Channel.create({'name': 'Hijacked'})
        hijacked.write({'ai_bot_partner_id': admin.id})
        default_bot = self.env.ref('im_livechat_ai.partner_ai_bot')
        default_bot.name = LEGACY_BOT_NAME
        Channel.search([]).write({'ai_enabled': False})
        module = self._load_post_migrate()
        self.assertIn(hijacked.id, module.unlink_user_bot_partners(self.env.cr))
        renamed = module.rename_legacy_bot_name(self.env.cr)
        module.tag_bot_partners(self.env.cr)
        self.env.invalidate_all()
        self.assertIn(legacy.id, renamed)
        self.assertNotIn(custom.id, renamed)
        self.assertEqual(legacy.ai_bot_name, DEFAULT_BOT_NAME)
        self.assertEqual(legacy.ai_bot_partner_id.name, DEFAULT_BOT_NAME)
        self.assertEqual(custom.ai_bot_name, 'WoowTech AI')
        self.assertEqual(default_bot.name, DEFAULT_BOT_NAME)
        self.assertFalse(hijacked.ai_bot_partner_id)
        self.assertEqual(admin.name, admin_name)
        self.assertTrue(admin.active)
