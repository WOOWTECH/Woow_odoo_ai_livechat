# -*- coding: utf-8 -*-
"""The backend decides the handoff (owner decision 40, as woow_ai_contact_livechat).

openai backends end their reply with ``[[handoff]]``; n8n answers
``"handoff": true`` (optionally ``handoff_reason``) or ends ``output`` with
the marker. Odoo removes the marker, posts the text (or the channel's handoff
message), then pauses, records the handoff on the job and notifies the
operators. Keywords are an optional pre-filter, empty by default. All HTTP
is mocked.
"""

import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger

from odoo.addons.im_livechat_ai.models.discuss_channel import HANDOFF_SYSTEM_LINE, strip_handoff_marker
from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    DEFAULT_HANDOFF_MESSAGE,
    HANDOFF_KEYWORDS_1_1_0,
)

from .common import TEST_BACKEND, TEST_N8N_BACKEND, ok_response
from .test_backend import _load_migration

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'


def json_response(data, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = data
    return response


class BackendHandoffCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator_user = new_test_user(
            cls.env, login='bh_operator', name='客服小明',
            groups='base.group_user,im_livechat.im_livechat_group_user',
        )
        cls.operator = cls.operator_user.partner_id
        Channel = cls.env['im_livechat.channel']
        common = {'ai_enabled': True, 'ai_max_retries': 0, 'ai_handoff_keywords': False,
                  'user_ids': [(6, 0, cls.operator_user.ids)]}
        cls.openai = Channel.create({**TEST_BACKEND, **common, 'name': '官網客服',
                                     'ai_system_prompt': 'You are the shop assistant.'})
        cls.n8n = Channel.create({**TEST_N8N_BACKEND, **common, 'name': 'n8n 客服'})
        cls.ResPartner = type(cls.env['res.partner'])

    def new_session(self, channel):
        guest = self.env['mail.guest'].create({'name': '訪客'})
        session = self.env['discuss.channel'].create({
            'name': 'Handoff Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.operator.id, 'livechat_channel_id': channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator.id})],
        })
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'guest_id': guest.id})
        return session, guest

    def visitor_says(self, session, guest, text):
        return self.env['mail.message'].create({
            'body': text, 'model': 'discuss.channel', 'res_id': session.id, 'message_type': 'comment',
            'author_id': False, 'author_guest_id': guest.id,
        })

    def ask(self, text, response, channel=None, side_effect=None):
        """A visitor message answered by the mocked backend; returns (session, job, post mock, bus mock)."""
        channel = channel or self.openai
        session, guest = self.new_session(channel)
        message = self.visitor_says(session, guest, text)
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        with patch(POST) as post, patch.object(self.ResPartner, '_bus_send', autospec=True) as bus:
            if side_effect:
                post.side_effect = side_effect
            else:
                post.return_value = response
            job._run_job(commit=False)
        return session, job, post, bus

    @staticmethod
    def request_body(post):
        kwargs = post.call_args.kwargs
        return json.loads(kwargs['data']) if kwargs.get('data') else kwargs['json']

    def bot_messages(self, session, channel=None):
        channel = channel or session.livechat_channel_id
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', session.id),
            ('author_id', '=', channel.ai_bot_partner_id.id),
        ], order='id')

    def odoobot_messages(self, partner):
        odoobot = self.env.ref('base.partner_root')
        chats = self.env['discuss.channel'].search([
            ('channel_type', '=', 'chat'),
            ('channel_member_ids.partner_id', '=', partner.id),
            ('channel_member_ids.partner_id', '=', odoobot.id),
        ])
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', 'in', chats.ids), ('author_id', '=', odoobot.id),
        ], order='id')

    @staticmethod
    def notifications(bus):
        return [c for c in bus.call_args_list if c.args[1] == 'simple_notification']

    def assert_handed_over(self, session, job, bus, reason=None):
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(job.handoff_source, 'backend')
        self.assertEqual(job.handoff_reason or None, reason)
        self.assertTrue(session._ai_is_paused())
        self.assertEqual(session.ai_pause_reason, 'handoff')
        self.assertEqual(len(self.notifications(bus)), 1)
        self.assertIn('要找真人客服', self.odoobot_messages(self.operator)[-1].body)


class TestN8nHandoff(BackendHandoffCase):

    def test_n8n_handoff_with_text(self):
        session, job, _post, bus = self.ask('可以轉接專人嗎', json_response({
            'output': '好的，我幫您轉接專人，請稍候。', 'handoff': True, 'handoff_reason': '訪客要求專人'}),
            channel=self.n8n)
        replies = self.bot_messages(session)
        self.assertEqual(replies.mapped('body'), ['<p>好的，我幫您轉接專人，請稍候。</p>'])  # no fixed notice on top
        self.assertEqual(job.reply_message_id, replies)
        self.assert_handed_over(session, job, bus, reason='訪客要求專人')
        self.assertIn('訪客要求專人', self.odoobot_messages(self.operator)[-1].body)

    def test_n8n_handoff_without_text_posts_the_handoff_message(self):
        session, job, _post, bus = self.ask('我要跟人講話', json_response({'handoff': True}), channel=self.n8n)
        notice = self.bot_messages(session)
        self.assertEqual(notice.mapped('body'), ['<p>%s</p>' % DEFAULT_HANDOFF_MESSAGE])
        self.assertEqual(job.reply_message_id, notice)
        self.assert_handed_over(session, job, bus)

    def test_n8n_item_list_and_output_marker(self):
        session, job, _post, bus = self.ask('我要跟人講話', json_response([{'handoff': True, 'replies': ['馬上轉接']}]),
                                            channel=self.n8n)
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>馬上轉接</p>'])
        self.assert_handed_over(session, job, bus)
        # a Chat Trigger workflow can only answer "output": the marker works there too
        session, job, _post, bus = self.ask('可以轉接專人嗎', json_response([{'output': '好的，請稍候。\n[[handoff]]'}]),
                                            channel=self.n8n)
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>好的，請稍候。</p>'])
        self.assert_handed_over(session, job, bus)

    def test_n8n_handoff_ignores_skip_and_keeps_300_characters_of_reason(self):
        session, job, _post, bus = self.ask('找人', json_response(
            {'handoff': True, 'skip': True, 'handoff_reason': '長' * 400}), channel=self.n8n)
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>%s</p>' % DEFAULT_HANDOFF_MESSAGE])
        self.assert_handed_over(session, job, bus, reason='長' * 300)

    def test_n8n_handoff_must_be_the_boolean_true(self):
        session, job, _post, bus = self.ask('你好', json_response({'output': '您好', 'handoff': 'true'}),
                                            channel=self.n8n)
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>您好</p>'])
        self.assertFalse(job.handoff_source)
        self.assertFalse(session.ai_paused_until)

    def test_n8n_payload_says_handoff_available(self):
        _session, _job, post, _bus = self.ask('你好', json_response({'output': '您好'}), channel=self.n8n)
        self.assertIs(self.request_body(post)['channel']['livechat']['handoff_available'], True)
        self.n8n.user_ids = [(5, 0, 0)]
        _session, _job, post, _bus = self.ask('你好', json_response({'output': '您好'}), channel=self.n8n)
        self.assertIs(self.request_body(post)['channel']['livechat']['handoff_available'], False)


class TestOpenAiHandoff(BackendHandoffCase):

    def test_openai_marker_stripped_and_handed_over(self):
        session, job, post, bus = self.ask('可以轉接專人嗎', ok_response('好的，我幫您轉接真人客服，請稍候。\n\n[[HANDOFF]]'))
        reply = self.bot_messages(session)
        self.assertEqual(reply.mapped('body'), ['<p>好的，我幫您轉接真人客服，請稍候。</p>'])
        self.assertNotIn('handoff', reply.body.lower())
        self.assertEqual(job.reply_message_id, reply)
        self.assert_handed_over(session, job, bus)
        # the system line told the model how to hand over (operators exist), at the end, once
        system = self.request_body(post)['messages'][0]
        self.assertEqual(system['role'], 'system')
        self.assertTrue(system['content'].startswith('You are the shop assistant.'))
        self.assertTrue(system['content'].endswith(HANDOFF_SYSTEM_LINE))
        self.assertEqual(system['content'].count(HANDOFF_SYSTEM_LINE), 1)

    def test_openai_marker_only_posts_the_handoff_message(self):
        self.openai.ai_handoff_message = '好的，客服馬上來。'
        session, job, _post, bus = self.ask('我要跟人講話', ok_response('[[handoff]]'))
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>好的，客服馬上來。</p>'])
        self.assertEqual(job.reply_message_id, self.bot_messages(session))
        self.assert_handed_over(session, job, bus)

    def test_marker_variants(self):
        for text, expected in (
            ('好的 [[handoff]]', '好的'),
            ('好的，請稍候。[[Handoff]]', '好的，請稍候。'),
            ('第一行\n[[handoff]]\n\n\n第二行', '第一行\n\n第二行'),
            ('[[ handoff ]]\n請稍候', '請稍候'),
            ('請稍候 `[[handoff]]`', '請稍候'),
            ('請稍候 \\[\\[handoff\\]\\]', '請稍候'),
        ):
            self.assertEqual(strip_handoff_marker(text), (expected, True), text)
        for text in ('人工智慧是什麼', '[handoff]', '[[hand off]]', ''):
            self.assertEqual(strip_handoff_marker(text), (text, False), text)

    def test_no_marker_no_handoff(self):
        session, job, _post, bus = self.ask('人工智慧是什麼', ok_response('人工智慧是讓電腦學習與推理的技術。'))
        self.assertEqual(job.state, 'done', job.error)
        self.assertFalse(job.handoff_source)
        self.assertFalse(session.ai_paused_until)
        self.assertFalse(self.notifications(bus))
        self.assertFalse(self.odoobot_messages(self.operator))

    def test_no_system_line_without_operators(self):
        self.openai.write({'user_ids': [(5, 0, 0)], 'ai_system_prompt': False})
        _session, _job, post, _bus = self.ask('你好', ok_response('您好'))
        messages = self.request_body(post)['messages']
        self.assertNotIn('[[handoff]]', json.dumps(messages, ensure_ascii=False))
        self.assertEqual([m['role'] for m in messages], ['user'])  # no prompt, no event: no system message

    def test_answer_dropped_when_an_operator_wrote_meanwhile(self):
        session, guest = self.new_session(self.openai)
        message = self.visitor_says(session, guest, '可以轉接專人嗎')
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)

        def operator_writes(*_args, **_kwargs):
            session.message_post(body='我來了', author_id=self.operator.id, message_type='comment',
                                 subtype_xmlid='mail.mt_comment')
            return ok_response('好的 [[handoff]]')

        with patch(POST, side_effect=operator_writes), \
                patch.object(self.ResPartner, '_bus_send', autospec=True) as bus:
            job._run_job(commit=False)
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(job.handoff_source)
        self.assertFalse(self.bot_messages(session))
        self.assertEqual(session.ai_pause_reason, 'operator')  # the operator's pause, not a handoff
        self.assertFalse(self.notifications(bus))


class TestHandoffKeywordPrefilter(BackendHandoffCase):

    def test_default_keywords_empty(self):
        channel = self.env['im_livechat.channel'].create({'name': 'New site'})
        self.assertFalse(channel.ai_handoff_keywords)
        self.assertFalse(channel._ai_is_handoff_request('我要找真人客服'))

    def test_configured_keyword_still_hands_over_without_backend(self):
        self.openai.ai_handoff_keywords = '找真人'
        session, _guest = self.new_session(self.openai)
        visitor = self.env['res.partner'].create({'name': '訪客 A'})
        session.message_post(body='我要找真人', author_id=visitor.id, message_type='comment',
                             subtype_xmlid='mail.mt_comment')
        job = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])
        self.assertEqual(job.kind, 'handoff')
        with patch(POST) as post, patch.object(self.ResPartner, '_bus_send', autospec=True):
            job._run_job(commit=False)
        post.assert_not_called()
        self.assertEqual(job.handoff_source, 'keyword')
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>%s</p>' % DEFAULT_HANDOFF_MESSAGE])

    def test_without_keywords_the_request_goes_to_the_backend(self):
        session, job, post, bus = self.ask('我要找真人客服', ok_response('好的，幫您轉接。\n[[handoff]]'))
        post.assert_called_once()
        self.assert_handed_over(session, job, bus)

    def test_migration_clears_the_1_1_0_default_list(self):
        post = _load_migration('18.0.1.2.0', 'post-migrate')
        self.assertEqual(post.HANDOFF_KEYWORDS_1_1_0, HANDOFF_KEYWORDS_1_1_0)
        Channel = self.env['im_livechat.channel']
        default = Channel.create({'name': 'Default', 'ai_handoff_keywords': HANDOFF_KEYWORDS_1_1_0})
        spaced = Channel.create({'name': 'Spaced', 'ai_handoff_keywords': HANDOFF_KEYWORDS_1_1_0.replace(',', ', ')})
        custom = Channel.create({'name': 'Custom', 'ai_handoff_keywords': '專人,主管'})
        self.env.flush_all()
        with self.assertLogs(post.__name__, level='WARNING') as logs:
            post.migrate(self.env.cr, '18.0.1.1.0')
        self.env.invalidate_all()
        self.assertFalse(default.ai_handoff_keywords)
        self.assertFalse(spaced.ai_handoff_keywords)
        self.assertEqual(custom.ai_handoff_keywords, '專人,主管')
        self.assertIn('handoff keywords cleared on', '\n'.join(logs.output))
        with mute_logger(post.__name__):
            post.migrate(self.env.cr, '18.0.1.1.0')  # idempotent
        self.env.invalidate_all()
        self.assertEqual(custom.ai_handoff_keywords, '專人,主管')
