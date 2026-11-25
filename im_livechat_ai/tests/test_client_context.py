# -*- coding: utf-8 -*-
"""A client's context never sets a field of a record this module creates.

Odoo's /mail/message/post applies the context the client sends, also for a
public guest, and create() takes default_<field> keys from the context. The
trigger, the reply job, the call log, the operator alert and the session drop
those keys; a channel's AI fields stay for Live Chat managers even through
default_ai_* keys. All HTTP is mocked; no real key."""

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user

from .common import TEST_BACKEND, make_ai_channel, ok_response

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'

FORGED_JOB = {
    'default_state': 'running',
    'default_started_at': '2099-01-01 00:00:00',
    'default_attempts': 3,
    'default_kind': 'notice',
    'default_deferred': True,
    'default_error': 'forged',
    'default_llm_calls': 7,
    'default_handoff_reason': 'forged',
}


class ClientContextCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(cls.env, login='ctx_operator', name='Context Operator',
                                     groups='base.group_user,im_livechat.im_livechat_group_user')
        cls.manager = new_test_user(cls.env, login='ctx_manager', name='Context Manager',
                                    groups='base.group_user,im_livechat.im_livechat_group_manager')
        cls.channel = make_ai_channel(cls.env, {**TEST_BACKEND, 'name': 'Context', 'ai_enabled': True,
                                                'ai_max_retries': 0})
        cls.channel.sudo().write({'user_ids': [(4, cls.operator.id)]})
        cls.Job = cls.env['im_livechat.ai.job'].sudo()

    def session(self):
        guest = self.env['mail.guest'].create({'name': 'Visitor'})
        session = self.env['discuss.channel'].create({
            'name': 'Context Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.operator.partner_id.id,
            'livechat_channel_id': self.channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator.partner_id.id})],
        })
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'guest_id': guest.id})
        return session, guest

    def guest_post(self, session, guest, text, **context):
        """A guest's post with the context a client may send to /mail/message/post."""
        public = self.env.ref('base.public_user')
        return session.with_user(public).with_context(guest=guest, **context).sudo().message_post(
            body=text, message_type='comment', subtype_xmlid='mail.mt_comment')

    def assertPlainJob(self, job):
        self.assertEqual(len(job), 1)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.attempts, 0)
        self.assertFalse(job.started_at)
        self.assertEqual(job.kind, 'reply')
        self.assertFalse(job.deferred)
        self.assertFalse(job.error)
        self.assertEqual(job.llm_calls, 0)
        self.assertFalse(job.handoff_reason)
        self.assertEqual(job.livechat_channel_id, self.channel)


class TestForgedJobDefaults(ClientContextCase):

    def test_guest_post_ignores_default_keys(self):
        """A forged running job with a future start would pin the channel's cap for good."""
        session, guest = self.session()
        self.guest_post(session, guest, '你好', **FORGED_JOB)
        self.assertPlainJob(self.Job.search([('discuss_channel_id', '=', session.id)]))

    def test_direct_create_ignores_default_keys(self):
        session, guest = self.session()
        message = self.env['mail.message'].create({
            'body': 'hi', 'model': 'discuss.channel', 'res_id': session.id, 'message_type': 'comment',
            'author_guest_id': guest.id})
        job = self.Job.with_context(**FORGED_JOB)._create_for_message(session, message)
        self.assertPlainJob(job)

    @patch(POST)
    def test_forged_attempts_never_give_up_without_a_call(self, mock_post):
        """default_attempts=3 made every message an exhausted job: an error message, a failed reply and an
        operator alert with no backend call."""
        mock_post.return_value = ok_response('好的')
        session, guest = self.session()
        ResPartner = type(self.env['res.partner'])
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send:
            self.guest_post(session, guest, '請問營業時間？', default_attempts=3)
            job = self.Job.search([('discuss_channel_id', '=', session.id)])
            job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.attempts, 1)
        self.assertEqual(mock_post.call_count, 1)
        alerts = [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']
        self.assertFalse(alerts, 'no operator alert')
        usage = self.env['im_livechat.ai.usage.month'].sudo().search([('livechat_channel_id', '=', self.channel.id)])
        self.assertEqual((usage.replies_ok, usage.replies_failed), (1, 0))

    def test_call_log_ignores_default_keys(self):
        session, _guest = self.session()
        log = self.channel.with_context(default_request_payload='forged', default_response_payload='forged',
                                        default_error_message='forged', default_prompt_tokens=999)._create_api_log(
            'ok', channel=self.channel, discuss_channel=session)
        self.assertEqual(log.status, 'ok')
        self.assertFalse(log.request_payload)
        self.assertFalse(log.response_payload)
        self.assertFalse(log.error_message)
        self.assertEqual(log.prompt_tokens, 0)

    def test_operator_alert_chat_ignores_default_keys(self):
        """Blocked channel: the OdooBot chat of the operator alert is created in the visitor's request."""
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_ai_connection SET api_key_secret = NULL WHERE id = %s",
                            (self.channel.sudo().ai_assistant_id.connection_id.id,))
        self.env.invalidate_all()
        session, guest = self.session()
        chats_before = self.env['discuss.channel'].sudo().search([('channel_type', '=', 'chat')])
        self.guest_post(session, guest, '你好', default_description='forged', default_name='forged')
        self.assertFalse(self.Job.search([('discuss_channel_id', '=', session.id)]))
        chats = self.env['discuss.channel'].sudo().search([('channel_type', '=', 'chat')]) - chats_before
        self.assertTrue(chats, 'the operator got an OdooBot chat')
        self.assertFalse(chats.filtered(lambda c: c.description == 'forged' or c.name == 'forged'))

    def test_session_ignores_default_ai_keys(self):
        session = self.env['discuss.channel'].with_context(
            default_ai_paused_until='2099-01-01 00:00:00', default_ai_pause_reason='handoff',
            default_ai_blocked_notice_sent=True, default_ai_pause_notice_sent=True,
        ).create({'name': 'Forged', 'channel_type': 'livechat', 'livechat_channel_id': self.channel.id,
                  'livechat_operator_id': self.operator.partner_id.id})
        self.assertFalse(session.ai_paused_until)
        self.assertFalse(session.ai_pause_reason)
        self.assertFalse(session.ai_blocked_notice_sent)
        self.assertFalse(session.ai_pause_notice_sent)


class TestChannelDefaultKeys(ClientContextCase):
    """Only Live Chat managers set up a channel's AI, also through default_ai_* keys (review #3)."""

    def test_operator_cannot_set_ai_through_default_keys(self):
        assistant = self.channel.sudo().ai_assistant_id
        Channel = self.env['im_livechat.channel'].with_user(self.operator)
        with self.assertRaises(AccessError):
            Channel.with_context(default_ai_enabled=True, default_ai_assistant_id=assistant.id).create(
                {'name': 'Operator channel'})
        with self.assertRaises(AccessError):
            Channel.with_context(default_ai_welcome_message='forged').create({'name': 'Operator channel'})
        # without AI keys an operator still creates channels (core rights)
        self.assertTrue(Channel.create({'name': 'Plain channel'}))

    def test_manager_may_use_default_keys(self):
        assistant = self.channel.sudo().ai_assistant_id
        channel = self.env['im_livechat.channel'].with_user(self.manager).with_context(
            default_ai_enabled=True, default_ai_assistant_id=assistant.id).create({'name': 'Manager channel'})
        self.assertTrue(channel.sudo().ai_enabled)
        self.assertEqual(channel.sudo().ai_assistant_id, assistant)
