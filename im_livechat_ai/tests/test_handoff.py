# -*- coding: utf-8 -*-
"""Package D (P1-1, P1-2, P1-3, P2-6, P2-7): who is a visitor, human handoff,
history roles, bot partner timing and the Chinese default error message.

All HTTP is mocked; no real key is used. Jobs run synchronously in the test
transaction with _run_job(commit=False).
"""

import importlib.util
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.im_livechat_ai.models.discuss_channel import (
    OPERATOR_SYSTEM_NOTE,
)
from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    DEFAULT_ERROR_MESSAGE,
    DEFAULT_HANDOFF_KEYWORDS,
    DEFAULT_HANDOFF_MESSAGE,
    DEFAULT_HANDOFF_PAUSE_MINUTES,
    ICP_API_KEY,
    LEGACY_ERROR_MESSAGE,
)

from .common import setup_litellm

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'


def _ok(content='您好，這是 AI 回覆'):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': {'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
    }
    response.raise_for_status = MagicMock()
    return response


class HandoffCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Handoff Channel',
            'ai_enabled': True,
            'ai_model': 'chatgpt-pool/gpt-6-sol',
            'ai_system_prompt': '你是客服助理。',
        })
        cls.bot = cls.channel.ai_bot_partner_id
        cls.operator_user = new_test_user(
            cls.env, login='handoff_operator', name='王小明',
            groups='base.group_user,im_livechat.im_livechat_group_user',
        )
        cls.operator = cls.operator_user.partner_id
        cls.portal_user = new_test_user(
            cls.env, login='handoff_portal', name='入口網站客戶', groups='base.group_portal',
        )
        cls.portal = cls.portal_user.partner_id
        cls.visitor = cls.env['res.partner'].create({'name': 'Handoff Visitor'})
        cls.session = cls._new_session()
        cls.Job = cls.env['im_livechat.ai.job']

    @classmethod
    def _new_session(cls, operator=None, name='Handoff Session'):
        return cls.env['discuss.channel'].create({
            'name': name,
            'channel_type': 'livechat',
            'livechat_operator_id': (operator or cls.operator).id,
            'livechat_channel_id': cls.channel.id,
        })

    def _post(self, body, author, session=None, message_type='comment'):
        return (session or self.session).message_post(
            body=body,
            author_id=author.id,
            message_type=message_type,
            subtype_xmlid='mail.mt_comment',
        )

    def _guest_post(self, body, session=None):
        session = session or self.session
        guest = self.env['mail.guest'].create({'name': 'Website Visitor'})
        public = self.env.ref('base.public_user')
        # sudo: like the livechat controller, guests post through sudo.
        message = session.with_user(public).with_context(guest=guest).sudo().message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_comment',
        )
        self.assertEqual(message.author_guest_id, guest)
        return message

    def _jobs(self, session=None):
        return self.Job.search([('discuss_channel_id', '=', (session or self.session).id)], order='id')

    def _bot_messages(self, session=None):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', (session or self.session).id),
            ('author_id', '=', self.bot.id),
        ], order='id')

    def _odoobot_chat_messages(self, partner):
        odoobot = self.env.ref('base.partner_root')
        chats = self.env['discuss.channel'].search([
            ('channel_type', '=', 'chat'),
            ('channel_member_ids.partner_id', '=', partner.id),
            ('channel_member_ids.partner_id', '=', odoobot.id),
        ])
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'),
            ('res_id', 'in', chats.ids),
            ('author_id', '=', odoobot.id),
        ])


class TestVisitorDetection(HandoffCase):
    """P1-1: visitor = guest, no author, or no internal (non-share) user."""

    def _kind(self, author=None, session=None):
        msg = self.env['mail.message'].create({
            'body': 'x', 'model': 'discuss.channel', 'res_id': (session or self.session).id,
            'message_type': 'comment', 'author_id': author.id if author else False,
        })
        return (session or self.session)._ai_author_kind(msg)

    def test_portal_user_is_visitor(self):
        self.assertEqual(self._kind(self.portal), 'visitor')

    def test_public_user_partner_is_visitor(self):
        public = self.env.ref('base.public_user').partner_id
        self.assertEqual(self._kind(public), 'visitor')

    def test_anonymous_and_plain_partner_are_visitors(self):
        self.assertEqual(self._kind(None), 'visitor')
        self.assertEqual(self._kind(self.visitor), 'visitor')

    def test_internal_user_is_operator(self):
        self.assertEqual(self._kind(self.operator), 'operator')
        other_internal = new_test_user(self.env, login='handoff_other_internal', groups='base.group_user')
        self.assertEqual(self._kind(other_internal.partner_id), 'operator')

    def test_archived_internal_user_is_still_operator(self):
        former = new_test_user(self.env, login='handoff_former', groups='base.group_user')
        partner = former.partner_id
        former.active = False
        self.assertEqual(self._kind(partner), 'operator')

    def test_bots_are_excluded(self):
        self.assertEqual(self._kind(self.bot), 'bot')
        self.assertEqual(self._kind(self.env.ref('im_livechat_ai.partner_ai_bot')), 'bot')
        self.assertEqual(self._kind(self.env.ref('base.partner_root')), 'bot')

    def test_session_operator_without_user_is_not_visitor(self):
        """An Odoo chatbot is the session operator but has no user."""
        chatbot = self.env['res.partner'].create({'name': 'Chatbot', 'active': False})
        session = self._new_session(operator=chatbot, name='Chatbot Session')
        self.assertEqual(self._kind(chatbot, session=session), 'bot')
        self.assertFalse(session._should_trigger_ai_response(
            self.env['mail.message'].create({
                'body': 'x', 'model': 'discuss.channel', 'res_id': session.id,
                'message_type': 'comment', 'author_id': chatbot.id,
            })))

    @patch(POST)
    def test_portal_user_gets_a_reply(self, mock_post):
        mock_post.return_value = _ok('您好，入口網站客戶')
        msg = self._post('我的訂單到哪了？', self.portal)
        job = self._jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.message_id, msg)
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertIn('您好，入口網站客戶', job.reply_message_id.body)
        sent = mock_post.call_args.kwargs['json']['messages']
        self.assertIn({'role': 'user', 'content': '我的訂單到哪了？'}, sent)

    @patch(POST)
    def test_portal_user_posting_as_himself_gets_a_job(self, mock_post):
        """Posted with the portal user's own env (member of the session)."""
        mock_post.return_value = _ok()
        self.session.add_members(partner_ids=self.portal.ids)
        self.session.with_user(self.portal_user).message_post(
            body='自己登入發問', message_type='comment', subtype_xmlid='mail.mt_comment',
        )
        job = self._jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.message_id.author_id, self.portal)

    def test_internal_user_message_creates_no_job(self):
        self._post('我是內部同仁', self.operator)
        self.assertFalse(self._jobs())


class TestOperatorPause(HandoffCase):
    """P1-2: an operator's post pauses the AI for the session."""

    def test_operator_post_pauses_session(self):
        before = fields.Datetime.now()
        self._post('您好，我是客服王小明', self.operator)
        self.assertTrue(self.session._ai_is_paused())
        expected = before + timedelta(minutes=DEFAULT_HANDOFF_PAUSE_MINUTES)
        self.assertAlmostEqual(self.session.ai_paused_until, expected, delta=timedelta(seconds=5))
        # While paused, visitor messages create no job.
        self._post('謝謝', self.visitor)
        self._guest_post('還有問題')
        self.assertFalse(self._jobs())

    def test_pause_minutes_per_channel(self):
        self.channel.ai_handoff_pause_minutes = 5
        before = fields.Datetime.now()
        self._post('我來處理', self.operator)
        self.assertAlmostEqual(self.session.ai_paused_until, before + timedelta(minutes=5),
                               delta=timedelta(seconds=5))

    def test_pause_minutes_range(self):
        with self.assertRaises(ValidationError):
            self.channel.ai_handoff_pause_minutes = 0
        with self.assertRaises(ValidationError):
            self.channel.ai_handoff_pause_minutes = 1441

    def test_ai_resumes_after_pause(self):
        self._post('我來處理', self.operator)
        self.session.sudo().ai_paused_until = fields.Datetime.now() - timedelta(seconds=1)
        self._post('客服不在了嗎？', self.visitor)
        self.assertEqual(len(self._jobs()), 1)

    def test_other_session_not_paused(self):
        other = self._new_session(name='Other Session')
        self._post('我來處理', self.operator)
        self._post('hi', self.visitor, session=other)
        self.assertFalse(other.ai_paused_until)
        self.assertEqual(len(self._jobs(other)), 1)

    def test_bot_odoobot_and_notifications_do_not_pause(self):
        self.channel._create_bot_message(self.env, self.session, 'AI 回覆')
        self._post('系統訊息', self.env.ref('base.partner_root'))
        self._post('王小明加入對話', self.operator, message_type='notification')
        self.assertFalse(self.session.ai_paused_until)

    def test_pause_never_shortened(self):
        later = fields.Datetime.now() + timedelta(hours=2)
        self.session.sudo().ai_paused_until = later
        self._post('我來處理', self.operator)
        self.assertEqual(self.session.ai_paused_until, later)

    def test_no_pause_when_ai_disabled(self):
        self.channel.ai_enabled = False
        self._post('我來處理', self.operator)
        self.assertFalse(self.session.ai_paused_until)

    @patch(POST)
    def test_pending_job_skipped_after_operator_post(self, mock_post):
        mock_post.return_value = _ok()
        self._post('請問有貨嗎？', self.visitor)
        job = self._jobs()
        self._post('有的，我幫您查', self.operator)
        job._run_job(commit=False)
        self.assertEqual(job.state, 'skipped')
        self.assertIn('真人客服', job.error)
        mock_post.assert_not_called()
        self.assertFalse(self._bot_messages())

    @patch(POST)
    def test_operator_post_during_llm_call_drops_answer(self, mock_post):
        def operator_answers_meanwhile(*args, **kwargs):
            self._post('我已經回覆您了', self.operator)
            return _ok('AI 的回答')

        mock_post.side_effect = operator_answers_meanwhile
        self._post('請問價格？', self.visitor)
        job = self._jobs()
        job._run_job(commit=False)
        self.assertEqual(job.state, 'skipped')
        self.assertEqual(mock_post.call_count, 1)
        self.assertFalse(self._bot_messages())


class TestKeywordHandoff(HandoffCase):
    """P1-2: a visitor asking for a human gets one notice, operators are told."""

    @patch(POST)
    def test_keyword_handoff_flow(self, mock_post):
        ResPartner = type(self.env['res.partner'])
        before = fields.Datetime.now()
        msg = self._post('我要找真人客服', self.visitor)
        job = self._jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.kind, 'handoff')
        self.assertEqual(job.message_id, msg)
        self.assertTrue(self.session._ai_is_paused())
        self.assertGreaterEqual(self.session.ai_paused_until,
                                before + timedelta(minutes=DEFAULT_HANDOFF_PAUSE_MINUTES - 1))
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send:
            job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        mock_post.assert_not_called()
        self.assertEqual(len(self._bot_messages()), 1)
        self.assertIn(DEFAULT_HANDOFF_MESSAGE, self._bot_messages().body)
        self.assertEqual(job.reply_message_id, self._bot_messages())
        # Operators: OdooBot chat message and a sticky toast.
        notes = self._odoobot_chat_messages(self.operator)
        self.assertEqual(len(notes), 1)
        self.assertIn('要求真人客服', notes.body)
        self.assertIn(f'discuss.channel_{self.session.id}', notes.body)
        calls = [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']
        self.assertEqual(len(calls), 1)
        self.assertIn(self.operator, calls[0].args[0])
        self.assertTrue(calls[0].args[2]['sticky'])
        # Replied once: more visitor messages (even with keywords) do nothing.
        self._post('真人在嗎？', self.visitor)
        self._post('還在嗎', self.visitor)
        self.assertEqual(len(self._jobs()), 1)
        job._run_job(commit=False)
        self.assertEqual(len(self._bot_messages()), 1)

    def test_guest_keyword_handoff(self):
        self._guest_post('請幫我轉人工')
        job = self._jobs()
        self.assertEqual(job.kind, 'handoff')
        job._run_job(commit=False)
        self.assertIn(DEFAULT_HANDOFF_MESSAGE, self._bot_messages().body)

    def test_default_keywords(self):
        self.assertEqual(self.channel.ai_handoff_keywords, DEFAULT_HANDOFF_KEYWORDS)
        hit = self.channel._ai_is_handoff_request
        for text in ('真人', '找客服', '人工服務', 'I need a HUMAN', 'Agent please', 'operator?'):
            self.assertTrue(hit(text), text)
        for text in ('請問價格', 'reagent', 'agents', 'cooperators', ''):
            self.assertFalse(hit(text), text)

    def test_custom_keywords_and_message(self):
        self.channel.write({
            'ai_handoff_keywords': '專人，主管\nmanager',
            'ai_handoff_message': '好的，專人馬上來。',
        })
        self.assertFalse(self.channel._ai_is_handoff_request('我要找客服'))
        self.assertTrue(self.channel._ai_is_handoff_request('請找主管'))
        self.assertTrue(self.channel._ai_is_handoff_request('your Manager'))
        self._post('請幫我找專人', self.visitor)
        job = self._jobs()
        self.assertEqual(job.kind, 'handoff')
        job._run_job(commit=False)
        self.assertIn('好的，專人馬上來。', self._bot_messages().body)

    def test_empty_keywords_disable_handoff(self):
        self.channel.ai_handoff_keywords = False
        self._post('我要找真人', self.visitor)
        self.assertEqual(self._jobs().kind, 'reply')
        self.assertFalse(self.session.ai_paused_until)

    def test_keyword_in_html_body(self):
        self._post('<p>麻煩<b>真人</b>回覆</p>', self.visitor)
        self.assertEqual(self._jobs().kind, 'handoff')

    def test_handoff_without_litellm(self):
        """The handoff notice needs no LLM, so it works without the key."""
        self.env['ir.config_parameter'].sudo().set_param(ICP_API_KEY, False)
        self._post('客服在嗎', self.visitor)
        job = self._jobs()
        self.assertEqual(job.kind, 'handoff')
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertTrue(self._bot_messages())

    @patch(POST)
    def test_handoff_supersedes_pending_reply(self, mock_post):
        mock_post.return_value = _ok()
        self._post('請問營業時間', self.visitor)
        self._post('算了，找真人', self.visitor)
        reply_job, handoff_job = self._jobs()
        self.assertEqual((reply_job.kind, handoff_job.kind), ('reply', 'handoff'))
        self.Job._run_chain_in_transaction(reply_job.id)
        self.assertEqual(reply_job.state, 'skipped')
        self.assertEqual(handoff_job.state, 'done')
        mock_post.assert_not_called()
        self.assertEqual(len(self._bot_messages()), 1)

    def test_handoff_not_repeated_by_recovery(self):
        """The notice and the done state are committed together: a finished
        handoff job is never run again."""
        self._post('真人客服', self.visitor)
        job = self._jobs()
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        job._run_job(commit=False)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(len(self._bot_messages()), 1)


class TestHistoryRoles(HandoffCase):
    """P1-3: operator messages are assistant messages marked as a colleague's."""

    def _msg(self, author, body):
        return self.env['mail.message'].create({
            'body': body, 'model': 'discuss.channel', 'res_id': self.session.id,
            'message_type': 'comment', 'author_id': author.id if author else False,
        })

    def test_role_mapping(self):
        self._msg(self.visitor, '請問可以退貨嗎？')
        self._msg(self.bot, '可以，七天內可退貨。')
        self._msg(self.operator, '我幫您安排明天收件。')
        self._msg(self.portal, '好的，謝謝')
        self._msg(None, '匿名訪客')
        messages = self.session._build_llm_messages(self.channel)
        self.assertEqual(messages[0]['role'], 'system')
        self.assertTrue(messages[0]['content'].startswith('你是客服助理。'))
        self.assertIn(OPERATOR_SYSTEM_NOTE, messages[0]['content'])
        self.assertEqual(messages[1:], [
            {'role': 'user', 'content': '請問可以退貨嗎？'},
            {'role': 'assistant', 'content': '可以，七天內可退貨。'},
            {'role': 'assistant', 'content': '[真人客服 王小明] 我幫您安排明天收件。'},
            {'role': 'user', 'content': '好的，謝謝'},
            {'role': 'user', 'content': '匿名訪客'},
        ])

    def test_no_operator_note_without_operator_messages(self):
        self._msg(self.visitor, 'hi')
        messages = self.session._build_llm_messages(self.channel)
        self.assertEqual(messages[0], {
            'role': 'system', 'content': '你是客服助理。\n\n' + self.channel._ai_product_guard(),
        })

    def test_operator_note_without_custom_prompt(self):
        self.channel.ai_system_prompt = False
        self._msg(self.operator, '稍等')
        self._msg(self.visitor, '好')
        messages = self.session._build_llm_messages(self.channel)
        self.assertEqual(messages[0], {
            'role': 'system',
            'content': OPERATOR_SYSTEM_NOTE + '\n\n' + self.channel._ai_product_guard(),
        })

    @patch(POST)
    def test_job_sends_operator_as_assistant(self, mock_post):
        """After the pause, the AI sees the operator's reply as assistant."""
        mock_post.return_value = _ok()
        self._post('有現貨嗎？', self.visitor)
        self._post('有現貨，今天出貨。', self.operator)
        self.session.sudo().ai_paused_until = fields.Datetime.now() - timedelta(seconds=1)
        self._post('那運費多少？', self.visitor)
        job = self._jobs()[-1]
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        sent = mock_post.call_args.kwargs['json']['messages']
        self.assertIn({'role': 'assistant', 'content': '[真人客服 王小明] 有現貨，今天出貨。'}, sent)
        self.assertEqual(sent[-1], {'role': 'user', 'content': '那運費多少？'})
        self.assertIn(OPERATOR_SYSTEM_NOTE, sent[0]['content'])


class TestBotPartnerTiming(TransactionCase):
    """P2-6: the bot partner is created when AI is enabled, never while replying."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.category = cls.env.ref('im_livechat_ai.partner_category_ai_bot')

    def test_created_with_ai_enabled(self):
        channel = self.env['im_livechat.channel'].create({
            'name': 'Bot On Create', 'ai_enabled': True, 'ai_bot_name': '小幫手',
        })
        bot = channel.ai_bot_partner_id
        self.assertTrue(bot)
        self.assertEqual(bot.name, '小幫手')
        self.assertFalse(bot.active, 'bot partners are archived (hidden from contacts)')
        self.assertIn(self.category, bot.category_id)
        self.assertEqual(self.category.name, 'AI 機器人')

    def test_not_created_without_ai(self):
        channel = self.env['im_livechat.channel'].create({'name': 'No AI'})
        self.assertFalse(channel.ai_bot_partner_id)

    def test_created_when_ai_enabled_later_and_renamed(self):
        channel = self.env['im_livechat.channel'].create({'name': 'Enable Later'})
        channel.write({'ai_enabled': True})
        bot = channel.ai_bot_partner_id
        self.assertTrue(bot)
        channel.write({'ai_bot_name': '新名字'})
        self.assertEqual(channel.ai_bot_partner_id, bot)
        self.assertEqual(bot.name, '新名字')
        # Disabling and re-enabling keeps the same partner.
        channel.write({'ai_enabled': False})
        channel.write({'ai_enabled': True})
        self.assertEqual(channel.ai_bot_partner_id, bot)

    def test_manager_enabling_ai_creates_bot(self):
        manager = new_test_user(self.env, login='bot_timing_manager',
                                groups='base.group_user,im_livechat.im_livechat_group_manager')
        channel = self.env['im_livechat.channel'].with_user(manager).create({
            'name': 'Manager Channel', 'ai_enabled': True,
        })
        self.assertTrue(channel.sudo().ai_bot_partner_id)

    def test_default_bot_is_archived_and_tagged(self):
        default_bot = self.env.ref('im_livechat_ai.partner_ai_bot')
        self.assertFalse(default_bot.active)
        self.assertIn(self.category, default_bot.category_id)

    @patch(POST)
    def test_reply_processing_never_creates_or_writes_bot(self, mock_post):
        mock_post.return_value = _ok('回覆')
        channel = self.env['im_livechat.channel'].create({
            'name': 'Lost Bot', 'ai_enabled': True, 'ai_model': 'chatgpt-pool/gpt-6-sol',
        })
        channel.ai_bot_partner_id.unlink()  # ondelete set null
        channel.invalidate_recordset()
        self.assertFalse(channel.ai_bot_partner_id)
        session = self.env['discuss.channel'].create({
            'name': 'Lost Bot Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.env.ref('base.partner_admin').id,
            'livechat_channel_id': channel.id,
        })
        visitor = self.env['res.partner'].create({'name': 'Lost Bot Visitor'})
        session.message_post(body='hi', author_id=visitor.id,
                             message_type='comment', subtype_xmlid='mail.mt_comment')
        Channel = type(self.env['im_livechat.channel'])
        job = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])
        partners_before = self.env['res.partner'].with_context(active_test=False).search_count([])
        with patch.object(Channel, '_get_or_create_bot_partner', autospec=True) as ensure:
            job._run_job(commit=False)
        ensure.assert_not_called()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.reply_message_id.author_id, self.env.ref('im_livechat_ai.partner_ai_bot'))
        self.assertFalse(channel.ai_bot_partner_id)
        self.assertEqual(
            self.env['res.partner'].with_context(active_test=False).search_count([]), partners_before)

    @patch(POST)
    def test_reply_authored_by_archived_bot(self, mock_post):
        mock_post.return_value = _ok('封存的機器人也能發言')
        channel = self.env['im_livechat.channel'].create({
            'name': 'Archived Bot', 'ai_enabled': True, 'ai_model': 'chatgpt-pool/gpt-6-sol',
        })
        session = self.env['discuss.channel'].create({
            'name': 'Archived Bot Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.env.ref('base.partner_admin').id,
            'livechat_channel_id': channel.id,
        })
        visitor = self.env['res.partner'].create({'name': 'Archived Bot Visitor'})
        session.message_post(body='hi', author_id=visitor.id,
                             message_type='comment', subtype_xmlid='mail.mt_comment')
        job = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.reply_message_id.author_id, channel.ai_bot_partner_id)
        # The bot's own message must not trigger another job (loop guard).
        self.assertEqual(len(self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])), 1)


class TestChineseErrorMessage(TransactionCase):
    """P2-7: Traditional Chinese default error message and its migration."""

    def _load_post_migrate(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'migrations', '18.0.1.1.0', 'post-migrate.py')
        spec = importlib.util.spec_from_file_location('im_livechat_ai_post_migrate_1_1_0', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_default_is_chinese(self):
        channel = self.env['im_livechat.channel'].create({'name': 'Default Error'})
        self.assertEqual(channel.ai_error_message, DEFAULT_ERROR_MESSAGE)
        self.assertIn('AI 客服暫時無法回覆', channel.ai_error_message)
        self.assertIn('聯絡方式', channel.ai_error_message)
        self.assertEqual(channel.ai_handoff_message, DEFAULT_HANDOFF_MESSAGE)
        self.assertEqual(channel.ai_handoff_pause_minutes, DEFAULT_HANDOFF_PAUSE_MINUTES)

    def test_migration_replaces_old_english_default(self):
        Channel = self.env['im_livechat.channel']
        old = Channel.create({'name': 'Old English', 'ai_error_message': LEGACY_ERROR_MESSAGE})
        empty = Channel.create({'name': 'Empty'})
        custom = Channel.create({'name': 'Custom', 'ai_error_message': '請撥 02-1234-5678'})
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_error_message = NULL WHERE id = %s",
                            (empty.id,))
        module = self._load_post_migrate()
        self.assertEqual(module.LEGACY_ERROR_MESSAGE, LEGACY_ERROR_MESSAGE)
        self.assertEqual(module.NEW_ERROR_MESSAGE, DEFAULT_ERROR_MESSAGE)
        changed = module.translate_error_message(self.env.cr)
        self.env.invalidate_all()
        self.assertIn(old.id, changed)
        self.assertIn(empty.id, changed)
        self.assertNotIn(custom.id, changed)
        self.assertEqual(old.ai_error_message, DEFAULT_ERROR_MESSAGE)
        self.assertEqual(empty.ai_error_message, DEFAULT_ERROR_MESSAGE)
        self.assertEqual(custom.ai_error_message, '請撥 02-1234-5678')

    def test_migration_archives_and_tags_existing_bots(self):
        setup_litellm(self.env)
        channel = self.env['im_livechat.channel'].create({'name': 'Old Bot', 'ai_enabled': True})
        bot = channel.ai_bot_partner_id
        category = self.env.ref('im_livechat_ai.partner_category_ai_bot')
        bot.write({'active': True, 'category_id': [(5, 0, 0)]})
        module = self._load_post_migrate()
        self.assertGreaterEqual(module.tag_bot_partners(self.env.cr), 2)
        self.env.invalidate_all()
        self.assertFalse(bot.active)
        self.assertIn(category, bot.category_id)
