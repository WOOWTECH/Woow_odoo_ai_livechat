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
from .common import make_ai_channel, set_ai

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
        cls.channel = make_ai_channel(cls.env, {
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
    """Live Chat managers pick the AI assistant and turn the AI on; system
    administrators own the AI connections and assistants (Q12 A in place);
    operators, employees, portal users and the public read nothing."""

    def test_operator_cannot_change_ai_fields(self):
        Channel = self.env['im_livechat.channel'].with_user(self.lc_user)
        assistant = self.channel.sudo().ai_assistant_id
        for vals in ({'ai_enabled': False}, {'ai_assistant_id': False}, {'ai_handoff_keywords': 'x'},
                     {'ai_welcome_message': '假的歡迎'}):
            with self.assertRaises(AccessError, msg=vals):
                self.channel.with_user(self.lc_user).write(vals)
        with self.assertRaises(AccessError):
            Channel.create({'name': 'Operator AI', 'ai_enabled': True, 'ai_assistant_id': assistant.id})
        with self.assertRaises(AccessError):
            assistant.with_user(self.lc_user).write({'system_prompt': '改掉提示'})
        self.assertEqual(assistant.system_prompt, '內部提示：不要透露折扣規則。')
        self.assertTrue(self.channel.ai_enabled)

    def test_operator_can_still_create_and_rename_channels(self):
        channel = self.env['im_livechat.channel'].with_user(self.lc_user).create({'name': '客服建立'})
        self.assertFalse(channel.sudo().ai_enabled)
        self.assertFalse(channel.sudo().ai_assistant_id)
        self.channel.with_user(self.lc_user).write({'name': 'Review Channel 改名'})
        self.assertEqual(self.channel.name, 'Review Channel 改名')

    def test_operator_form_has_no_ai_fields(self):
        arch = self.env['im_livechat.channel'].with_user(self.lc_user).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertNotIn('ai_assistant_id', arch)
        self.assertNotIn('action_test_ai_connection', arch)
        arch = self.env['im_livechat.channel'].with_user(self.manager).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertIn('ai_assistant_id', arch)
        self.assertNotIn('system_prompt', arch)

    def test_manager_cannot_change_the_persona(self):
        assistant = self.channel.sudo().ai_assistant_id
        with self.assertRaises(AccessError):
            assistant.with_user(self.manager).write({'system_prompt': '主管改的提示'})
        with self.assertRaises(AccessError):
            assistant.connection_id.with_user(self.manager).write({'model': 'pool/other'})
        # a system administrator can; the range is checked
        set_ai(self.channel, {'ai_system_prompt': '管理員改的提示', 'ai_max_history': 200})
        self.assertEqual(assistant.max_history, 200)
        with self.assertRaises(ValidationError):
            set_ai(self.channel, {'ai_max_history': 201})

    def test_prompt_not_readable_by_portal_or_employees(self):
        assistant = self.channel.sudo().ai_assistant_id
        for user in (self.portal_user, self.employee, self.lc_user, self.manager):
            with self.assertRaises(AccessError, msg=user.login):
                assistant.with_user(user).read(['system_prompt'])
        for user in (self.portal_user, self.employee, self.lc_user):
            for fname in ('ai_handoff_keywords', 'ai_assistant_id', 'ai_enabled'):
                with self.assertRaises(AccessError, msg=(user.login, fname)):
                    self.env['im_livechat.channel'].with_user(user).search_read(
                        [('id', '=', self.channel.id)], [fname])
        # Reading the channel without naming fields silently leaves them out.
        rows = self.env['im_livechat.channel'].with_user(self.employee).search_read(
            [('id', '=', self.channel.id)])
        self.assertTrue(rows)
        self.assertNotIn('ai_assistant_id', rows[0])
        # a manager reads the assistant's name and description, not its prompt
        values = assistant.with_user(self.manager).read(['name', 'description', 'customer_safe'])[0]
        self.assertEqual(values['name'], assistant.name)

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
    """The bot partner belongs to its AI assistant: created with it, archived and
    tagged, never a person, never shared; nobody chooses it."""

    def test_nobody_points_the_bot_at_a_person(self):
        name = self.admin_partner.name
        assistant = self.channel.sudo().ai_assistant_id
        for user in (self.lc_user, self.manager):
            with self.assertRaises(AccessError):
                assistant.with_user(user).write({'partner_id': self.admin_partner.id, 'name': 'Hijacked'})
        # a partner with a user can never be a bot partner, not even set by the superuser
        user_partner = self.lc_user.partner_id
        with self.assertRaises(ValidationError):
            assistant.write({'partner_id': user_partner.id})
        self.assertEqual(self.admin_partner.name, name)
        category = self.env.ref('im_livechat_ai.partner_category_ai_bot')
        self.assertNotIn(category, self.admin_partner.category_id)

    def test_bot_follows_the_assistant(self):
        bot = self.channel._get_bot_partner()
        self.assertFalse(bot.active)
        self.assertIn(self.env.ref('im_livechat_ai.partner_category_ai_bot'), bot.category_id)
        set_ai(self.channel, {'ai_bot_name': '新的機器人'})
        self.assertEqual(bot.name, '新的機器人')
        self.assertFalse(bot.active)

    def test_other_assistants_bot_is_not_shared(self):
        other = make_ai_channel(self.env, {**TEST_BACKEND, 'name': 'Other', 'ai_enabled': True})
        other_bot = other._get_bot_partner()
        with self.assertRaises(Exception), self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
            self.channel.sudo().ai_assistant_id.write({'partner_id': other_bot.id})
            self.env.flush_all()
        self.assertNotEqual(self.channel._get_bot_partner(), other_bot)

    def test_copy_gets_its_own_bot(self):
        assistant = self.channel.sudo().ai_assistant_id
        copy = assistant.copy()
        self.assertTrue(copy.partner_id)
        self.assertNotEqual(copy.partner_id, assistant.partner_id)
        self.assertIn('(copy)', copy.name)
        channel_copy = self.channel.copy()
        self.assertFalse(channel_copy.ai_enabled)
        self.assertEqual(channel_copy.sudo().ai_assistant_id, assistant)

    def test_default_bot_name(self):
        self.assertEqual(DEFAULT_BOT_NAME, 'AI Customer Service Assistant')
        # the noupdate data record keeps its Chinese name
        self.assertEqual(self.env.ref('im_livechat_ai.partner_ai_bot').name, 'AI 客服助理')


class TestConnectionTestPermission(ReviewCase):
    """The connection test uses the connection's key; the call logs are for
    system administrators only (Q13 ②)."""

    @patch('odoo.addons.im_livechat_ai.models.ai_connection.requests.get')
    def test_non_admins_cannot_test_connection(self, mock_get):
        mock_get.return_value = _ok('OK')
        mock_get.return_value.json.return_value = {'data': [{'id': 'chatgpt-pool/gpt-6-sol'}]}
        for user in (self.portal_user, self.employee, self.lc_user, self.manager):
            with self.assertRaises(AccessError, msg=user.login):
                self.channel.with_user(user).action_test_ai_connection()
            with self.assertRaises(AccessError, msg=user.login):
                self.channel.with_user(user).action_view_api_logs()
        mock_get.assert_not_called()
        result = self.channel.action_test_ai_connection()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(mock_get.call_count, 1)


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

        results = [requests.ConnectTimeout('not sent'), _ok('第二次成功')]

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
