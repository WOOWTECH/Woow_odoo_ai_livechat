# -*- coding: utf-8 -*-
"""What the runtime reads in 18.0.2.0.0: the channel's AI assistant and its AI
connection; the n8n body; the bot set; the blocked gate; the concurrency
re-raise; call logs and monthly usage. All HTTP is mocked; no real key."""

import json
from unittest.mock import MagicMock, patch

import psycopg2.errors
import requests

from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import mute_logger

from odoo.addons.im_livechat_ai.models import im_livechat_channel as ch

from .common import (TEST_BACKEND, TEST_N8N_BACKEND, make_ai_channel, make_assistant, make_connection, run_ai_job,
                     set_ai)

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
SLEEP = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep'
CHANNEL_LOGGER = 'odoo.addons.im_livechat_ai.models.im_livechat_channel'
JOB_LOGGER = 'odoo.addons.im_livechat_ai.models.livechat_ai_job'


def _json(data, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = data
    return response


def _ok(text='好的', usage=None):
    return _json({'choices': [{'message': {'content': text}}],
                  'usage': usage or {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}})


class RuntimeCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param('database.uuid', '0edfec80-1111-2222-3333-444455556666')
        cls.operator = new_test_user(cls.env, login='rt_operator', name='客服',
                                     groups='base.group_user,im_livechat.im_livechat_group_user')
        cls.channel = make_ai_channel(cls.env, {**TEST_BACKEND, 'name': '官網客服', 'ai_enabled': True,
                                                'ai_system_prompt': '助理提示', 'ai_max_retries': 0,
                                                'ai_bot_name': '官網助理'})
        cls.n8n = make_ai_channel(cls.env, {**TEST_N8N_BACKEND, 'name': 'n8n 客服', 'ai_enabled': True,
                                            'ai_max_retries': 0, 'ai_bot_name': 'n8n 助理'})

    def session(self, channel=None, text='請問營業時間？'):
        channel = channel or self.channel
        guest = self.env['mail.guest'].create({'name': '訪客'})
        session = self.env['discuss.channel'].create({
            'name': 'Runtime Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.operator.partner_id.id,
            'livechat_channel_id': channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator.partner_id.id})],
        })
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'guest_id': guest.id})
        message = self.env['mail.message'].create({
            'body': text, 'model': 'discuss.channel', 'res_id': session.id, 'message_type': 'comment',
            'author_id': False, 'author_guest_id': guest.id}) if text else None
        return session, guest, message

    def guest_post(self, session, guest, text):
        """A message posted by the guest visitor, as the live chat route does."""
        public = self.env.ref('base.public_user')
        return session.with_user(public).with_context(guest=guest).sudo().message_post(
            body=text, message_type='comment', subtype_xmlid='mail.mt_comment')


class TestRuntimeReads(RuntimeCase):

    @patch(POST)
    def test_request_comes_from_the_connection_and_the_assistant(self, mock_post):
        mock_post.return_value = _ok()
        set_ai(self.channel, {'ai_endpoint_url': 'https://gateway.example/v1/', 'ai_api_token': 'dummy-gw',
                              'ai_model': 'pool/model-x', 'ai_system_prompt': '新的提示', 'ai_max_history': 1})
        session, guest, _m = self.session()
        self.env['mail.message'].create({'body': '第二句', 'model': 'discuss.channel', 'res_id': session.id,
                                         'message_type': 'comment', 'author_id': False,
                                         'author_guest_id': guest.id})
        run_ai_job(self.env, session)
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://gateway.example/v1/chat/completions')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer dummy-gw')
        payload = json.loads(kwargs['data'])
        self.assertEqual(payload['model'], 'pool/model-x')
        self.assertEqual(payload['messages'], [{'role': 'system', 'content': '新的提示'},
                                               {'role': 'user', 'content': '第二句'}])

    @patch(POST)
    def test_n8n_body_contract_keys(self, mock_post):
        mock_post.return_value = _json({'reply': '好'})
        session, _guest, message = self.session(self.n8n)
        run_ai_job(self.env, session)
        body = json.loads(mock_post.call_args.kwargs['data'])
        assistant = self.n8n.sudo().ai_assistant_id
        self.assertEqual(body['assistant'], {'id': assistant.id, 'name': 'n8n 助理'})
        self.assertEqual(body['contact'], {'id': self.n8n.id, 'name': 'n8n 客服'})
        self.assertEqual(body['surface'], {'type': 'livechat', 'livechat_channel_id': self.n8n.id})
        self.assertNotIn('connection', body)
        for key in ('event', 'session_id', 'sessionId', 'action', 'chatInput', 'user', 'channel', 'system_prompt',
                    'message', 'history', 'events'):
            self.assertIn(key, body)
        self.assertEqual(body['events'], [])
        self.assertEqual(body['chatInput'], body['message']['text'])
        self.assertEqual(body['session_id'], 'woow-ai-0edfec80-%s' % session.id)

    @patch(POST)
    def test_logs_carry_the_connection_and_the_assistant(self, mock_post):
        mock_post.return_value = _ok()
        session, _guest, _message = self.session()
        job = run_ai_job(self.env, session)
        log = self.env['llm.api.log'].search([('discuss_channel_id', '=', session.id)])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.assistant_id, self.channel.sudo().ai_assistant_id)
        self.assertEqual(log.connection_id, self.channel.sudo().ai_assistant_id.connection_id)
        self.assertEqual(log.job_id, job)
        self.assertEqual((log.status, log.prompt_tokens, log.total_tokens), ('ok', 7, 10))
        self.assertFalse(log.request_payload, 'content only while the connection logs it')

    @patch(POST)
    def test_former_bot_partner_is_a_bot_turn(self, mock_post):
        """A message of a former 18.0.1.x channel bot (tagged, no user) is never a visitor."""
        mock_post.return_value = _ok()
        category = self.env.ref('im_livechat_ai.partner_category_ai_bot')
        former = self.env['res.partner'].create({'name': '舊機器人', 'active': False,
                                                 'category_id': [(4, category.id)]})
        session, guest, _m = self.session()
        message = self.env['mail.message'].create({'body': '我是舊機器人', 'model': 'discuss.channel',
                                                   'res_id': session.id, 'message_type': 'comment',
                                                   'author_id': former.id})
        self.assertFalse(session._is_visitor_message(message))
        conversation = session._ai_conversation(self.channel)
        self.assertEqual([e['kind'] for e in conversation['history']], ['visitor', 'bot'])
        # every assistant's partner is a bot too, also an archived assistant's
        other = make_assistant(self.env, make_connection(self.env, name='other'), name='別的')
        other.active = False
        self.assertIn(other.partner_id.id, session._ai_bot_ids())

    def test_bot_set_is_computed_once_per_conversation(self):
        session, guest, _m = self.session()
        for i in range(5):
            self.env['mail.message'].create({'body': 'm%s' % i, 'model': 'discuss.channel', 'res_id': session.id,
                                             'message_type': 'comment', 'author_id': False,
                                             'author_guest_id': guest.id})
        with patch.object(type(self.env['im_livechat.channel']), '_ai_bot_partner_ids',
                          autospec=True, side_effect=lambda *a, **k: set()) as bot_ids:
            session._ai_conversation(self.channel)
        self.assertEqual(bot_ids.call_count, 1)

    @patch(POST)
    def test_usage_month(self, mock_post):
        mock_post.return_value = _ok(usage={'prompt_tokens': 11, 'completion_tokens': 4})
        session, _g, _m = self.session()
        run_ai_job(self.env, session)
        session2, _g, _m = self.session()
        run_ai_job(self.env, session2)
        mock_post.return_value = _json({}, status=500)
        session3, _g, _m = self.session()
        with mute_logger(CHANNEL_LOGGER, JOB_LOGGER):
            run_ai_job(self.env, session3)
        usage = self.env['im_livechat.ai.usage.month'].search(
            [('assistant_id', '=', self.channel.sudo().ai_assistant_id.id)])
        self.assertEqual(len(usage), 1, 'one row per assistant, connection, surface, channel and month')
        self.assertEqual((usage.replies_ok, usage.replies_failed), (2, 1))
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.total_tokens), (22, 8, 30))
        self.assertEqual(usage.surface_type, 'livechat')
        self.assertEqual(usage.livechat_channel_id, self.channel)
        self.assertEqual(usage.scope_res_id, self.channel.id)
        self.assertEqual(usage.month.day, 1)


class TestInternalNotes(RuntimeCase):
    """Internal notes of colleagues never reach the backend; they still pause the AI."""

    def test_internal_note_not_in_history_and_pauses(self):
        session, guest, _m = self.session(text=None)
        self.guest_post(session, guest, '請問營業時間？')
        session.with_user(self.operator).message_post(body='內部：這位是 VIP', message_type='comment',
                                                      subtype_xmlid='mail.mt_note')
        self.assertTrue(session._ai_is_paused(), 'an operator note pauses the AI like a comment')
        self.env['mail.message'].create({'body': '標記為內部', 'model': 'discuss.channel', 'res_id': session.id,
                                         'message_type': 'comment', 'is_internal': True,
                                         'author_id': self.operator.partner_id.id})
        texts = ' '.join(entry['text'] for entry in session._ai_conversation(self.channel)['history'])
        self.assertIn('請問營業時間', texts)
        self.assertNotIn('VIP', texts)
        self.assertNotIn('標記為內部', texts)


class TestRateLimitAlerts(RuntimeCase):
    """After an HTTP 429 failure the operators are alerted at most once per channel
    per 10 minutes; the visitor gets the error message every time."""

    @patch(POST)
    def test_one_alert_per_flood(self, mock_post):
        mock_post.return_value = _json({}, status=429)
        set_ai(self.channel, {'ai_error_message': '忙線中，請稍後再試'})
        ResPartner = type(self.env['res.partner'])
        bot = self.channel._get_bot_partner()
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send, \
                mute_logger(CHANNEL_LOGGER, JOB_LOGGER):
            sessions = []
            for i in range(10):
                session, _g, _m = self.session(text='第 %s 位訪客' % i)
                sessions.append(session)
                job = run_ai_job(self.env, session)
                self.assertEqual(job.state, 'failed')
                self.assertTrue(job.error.startswith('HTTP 429'))
        alerts = [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']
        self.assertEqual(len(alerts), 1)
        for session in sessions:
            replies = session.message_ids.filtered(lambda m: m.author_id == bot)
            self.assertIn('忙線中', replies.body)
        # another kind of failure still alerts
        mock_post.return_value = _json({}, status=500)
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send, \
                mute_logger(CHANNEL_LOGGER, JOB_LOGGER):
            session, _g, _m = self.session(text='500')
            run_ai_job(self.env, session)
        self.assertEqual(len([c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']), 1)


class TestBlockedGate(RuntimeCase):
    """Contract §3.5 step 2: an AI-enabled channel that cannot answer queues no
    job; the conversation gets one notice per blocked period (a notification,
    never read as history) and the operators one alert per blocked period."""

    def _block(self):
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_ai_connection SET api_key_secret = NULL WHERE id = %s",
                            (self.channel.sudo().ai_assistant_id.connection_id.id,))
        self.env.invalidate_all()

    @patch(POST)
    def test_blocked_channel(self, mock_post):
        self._block()
        session, guest, _m = self.session(text=None)
        ch._BLOCKED_WARNED.pop(self.channel.id, None)
        ResPartner = type(self.env['res.partner'])
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send, \
                self.assertLogs(CHANNEL_LOGGER, level='WARNING') as logs:
            for text in ('第一句', '第二句'):
                self.guest_post(session, guest, text)
        mock_post.assert_not_called()
        self.assertFalse(self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)]))
        notices = session.message_ids.filtered(lambda m: m.message_type == 'notification'
                                               and 'cannot answer right now' in (m.body or ''))
        self.assertEqual(len(notices), 1, 'one notice per blocked period')
        self.assertTrue(session.ai_blocked_notice_sent)
        self.assertEqual(len([line for line in logs.output if 'cannot answer' in line]), 1)
        alerts = [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']
        self.assertEqual(len(alerts), 1, 'one operator alert per blocked period')
        self.assertIn('官網客服', alerts[0].args[2]['message'])
        # the notice is never part of what the backend gets
        texts = [entry['text'] for entry in session._ai_conversation(self.channel)['history']]
        self.assertNotIn('cannot answer right now', ' '.join(texts))
        # answering again ends the period: the next blocked period gets its notice and alert
        set_ai(self.channel, {'ai_api_token': 'dummy-again'})
        self.guest_post(session, guest, '第三句')
        self.assertFalse(session.ai_blocked_notice_sent)
        self.assertFalse(self.channel.sudo().ai_blocked_alert_sent)

    def test_server_warning_is_rate_limited(self):
        self._block()
        session, _g, message = self.session()
        ch._BLOCKED_WARNED.pop(self.channel.id, None)
        with self.assertLogs(CHANNEL_LOGGER, level='WARNING') as logs:
            for _i in range(3):
                self.channel._trigger_ai_response(session, message)
        self.assertEqual(len([line for line in logs.output if 'cannot answer' in line]), 1)

    def test_handoff_keyword_still_works_while_blocked(self):
        self._block()
        session, guest, _m = self.session(text=None)
        self.guest_post(session, guest, '我要找真人客服')
        job = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])
        self.assertEqual(job.kind, 'handoff')


class TestConcurrencyReRaise(RuntimeCase):
    """Contract §3.5 step 1 (L-12): a PostgreSQL concurrency error in the trigger
    is re-raised so Odoo retries the whole request; any other error is logged
    and never undoes the visitor's message."""

    def test_concurrency_errors_are_re_raised(self):
        session, guest, _m = self.session(text=None)
        Session = type(self.env['discuss.channel'])
        for error in (psycopg2.errors.SerializationFailure, psycopg2.errors.LockNotAvailable,
                      psycopg2.errors.DeadlockDetected):
            with self.subTest(error=error.__name__), \
                    patch.object(Session, '_ai_handle_new_message', side_effect=error('test')), \
                    self.assertRaises(error):
                self.guest_post(session, guest, 'hi')

    def test_other_errors_are_swallowed(self):
        session, guest, _m = self.session(text=None)
        Session = type(self.env['discuss.channel'])
        with patch.object(Session, '_ai_handle_new_message', side_effect=RuntimeError('boom')), \
                mute_logger('odoo.addons.im_livechat_ai.models.discuss_channel'):
            message = self.guest_post(session, guest, 'hi')
        self.assertTrue(message.exists())


class TestRetryAndDeadline(RuntimeCase):

    @patch(SLEEP)
    @patch(POST)
    def test_litellm_retries_only_not_sent_429_503(self, mock_post, mock_sleep):
        set_ai(self.channel, {'ai_max_retries': 2})
        status_503 = _json({}, status=503)
        mock_post.side_effect = [requests.ConnectTimeout('x'), status_503, _ok('第三次')]
        session, _g, _m = self.session()
        job = run_ai_job(self.env, session)
        self.assertEqual(job.state, 'done')
        self.assertEqual(mock_post.call_count, 3)
        for outcome in (requests.ReadTimeout('x'), _json({}, status=401)):
            with self.subTest(outcome=outcome):
                mock_post.reset_mock(side_effect=True, return_value=True)
                mock_post.side_effect = [outcome, _ok('不會')]
                session, _g, _m = self.session()
                with mute_logger(CHANNEL_LOGGER, JOB_LOGGER):
                    job = run_ai_job(self.env, session)
                self.assertEqual(job.state, 'failed')
                self.assertEqual(mock_post.call_count, 1)


class TestFailureAlertKinds(RuntimeCase):
    """One operator alert per kind of failure and channel in 10 minutes (review #7): a LiteLLM outage
    or an exhausted budget no longer sends one alert per visitor message."""

    def alerts(self, bus_send):
        return [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']

    @patch(POST)
    def test_one_alert_per_kind_of_failure(self, mock_post):
        ResPartner = type(self.env['res.partner'])
        mock_post.return_value = _json({}, status=500)
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send, \
                mute_logger(CHANNEL_LOGGER, JOB_LOGGER):
            for i in range(3):
                session, _g, _m = self.session(text='第 %s 位' % i)
                self.assertEqual(run_ai_job(self.env, session).state, 'failed')
        self.assertEqual(len(self.alerts(bus_send)), 1, 'one alert for three HTTP 500 failures')
        self.assertIn('not announced again', self.alerts(bus_send)[0].args[2]['message'])
        mock_post.side_effect = requests.ReadTimeout('read timeout')
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send, \
                mute_logger(CHANNEL_LOGGER, JOB_LOGGER):
            for _i in range(2):
                session, _g, _m = self.session(text='逾時')
                run_ai_job(self.env, session)
        self.assertEqual(len(self.alerts(bus_send)), 1, 'another kind of failure alerts once')

    def test_failure_kinds(self):
        Job = self.env['im_livechat.ai.job']
        self.assertEqual(Job._failure_kind('HTTP 500: the AI backend is failing (HTTPError)'), 'HTTP 500')
        self.assertEqual(Job._failure_kind('the backend did not answer in time (ReadTimeout)'),
                         'the backend did not answer in time (ReadTimeout)')
        self.assertEqual(Job._failure_kind('not processed within 30 minutes: given up'),
                         'not processed within 30 minutes')

    def test_aged_out_jobs_alert_once(self):
        ResPartner = type(self.env['res.partner'])
        jobs = self.env['im_livechat.ai.job']
        for i in range(3):
            session, _g, message = self.session(text='舊訊息 %s' % i)
            jobs |= self.env['im_livechat.ai.job']._create_for_message(session, message)
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_ai_job SET create_date = now() at time zone 'UTC' - interval '2 hours' "
                            "WHERE id IN %s", (tuple(jobs.ids),))
        self.env.invalidate_all()
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send, mute_logger(JOB_LOGGER):
            for job in jobs.sorted('id'):
                job._run_job(commit=False)
        self.assertEqual(set(jobs.mapped('state')), {'failed'})
        self.assertEqual(len(self.alerts(bus_send)), 1, 'one alert for the backlog the cron gives up')


class TestReplyCaps(RuntimeCase):
    """A message the cap of unfinished replies refuses gets the notice and one operator alert per period
    (review #8); the cap also holds for the whole database (review #15)."""

    def test_channel_cap_tells_the_visitor_and_the_operators(self):
        self.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '1')
        ResPartner = type(self.env['res.partner'])
        ch._FULL_WARNED.pop(self.channel.id, None)
        first, guest1, _m = self.session(text=None)
        second, guest2, _m = self.session(text=None)
        third, guest3, _m = self.session(text=None)
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send, \
                self.assertLogs(CHANNEL_LOGGER, level='WARNING') as logs:
            self.guest_post(first, guest1, '第一位')
            self.guest_post(second, guest2, '第二位')
            self.guest_post(second, guest2, '第二位再問')
            self.guest_post(third, guest3, '第三位')
        Job = self.env['im_livechat.ai.job']
        self.assertEqual(Job.search_count([('discuss_channel_id', '=', first.id)]), 1)
        self.assertFalse(Job.search_count([('discuss_channel_id', 'in', (second | third).ids)]))
        for session in (second, third):
            notices = session.message_ids.filtered(lambda m: m.message_type == 'notification'
                                                   and 'cannot answer right now' in (m.body or ''))
            self.assertEqual(len(notices), 1, 'one notice per session and period')
        alerts = [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']
        self.assertEqual(len(alerts), 1, 'one operator alert per period of the channel')
        self.assertIn('too many unfinished replies', alerts[0].args[2]['message'])
        self.assertEqual(len([line for line in logs.output if 'too many unfinished' in line]), 1,
                         'the warning is rate-limited')

    def test_database_cap(self):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('im_livechat_ai.max_active_jobs', '0')
        ICP.set_param('im_livechat_ai.max_active_jobs_total', '2')
        Job = self.env['im_livechat.ai.job']
        Job.search([('state', 'in', ('pending', 'running'))]).write({'state': 'skipped'})
        for channel in (self.channel, self.n8n):
            session, _g, message = self.session(channel=channel)
            self.assertTrue(Job._create_for_message(session, message))
        session, _g, message = self.session()
        with mute_logger(CHANNEL_LOGGER):
            self.assertFalse(Job._create_for_message(session, message), 'the third live reply is refused')
        ICP.set_param('im_livechat_ai.max_active_jobs_total', '0')
        self.assertTrue(Job._create_for_message(session, message), '0 turns the database cap off')


class TestAlertConcurrency(RuntimeCase):
    """The flag of the blocked alert is flushed inside its savepoint: a concurrent update is re-raised so
    Odoo retries the request, and the transaction stays usable (review #10)."""

    def test_flag_conflict_is_re_raised(self):
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_ai_connection SET api_key_secret = NULL WHERE id = %s",
                            (self.channel.sudo().ai_assistant_id.connection_id.id,))
        self.env.invalidate_all()
        session, guest, _m = self.session(text=None)
        Channel = type(self.env['im_livechat.channel'])
        original = Channel.flush_recordset

        def flush_recordset(records, fnames=None):
            if fnames == ['ai_blocked_alert_sent']:
                raise psycopg2.errors.SerializationFailure('concurrent update')
            return original(records, fnames)

        with patch.object(Channel, 'flush_recordset', flush_recordset), \
                self.assertRaises(psycopg2.errors.SerializationFailure), mute_logger(CHANNEL_LOGGER):
            self.guest_post(session, guest, '你好')
        self.env.cr.execute("SELECT 1")
        self.assertEqual(self.env.cr.fetchone(), (1,), 'the cursor is still usable')


class TestUnexpectedErrorFrames(RuntimeCase):
    """An unexpected exception in the reply path is logged with its type and code frames, never its text
    (review #12, contract 3.9)."""

    @patch(POST)
    def test_frames_without_text(self, mock_post):
        mock_post.side_effect = RuntimeError('sentinel-text-4242')
        session, _g, _m = self.session()
        with self.assertLogs(CHANNEL_LOGGER, level='WARNING') as logs, mute_logger(JOB_LOGGER):
            run_ai_job(self.env, session)
        lines = '\n'.join(logs.output)
        self.assertIn('RuntimeError', lines)
        self.assertIn('File "', lines, 'code frames')
        self.assertNotIn('sentinel-text-4242', lines)

    @patch(POST)
    def test_expected_failures_stay_one_line(self, mock_post):
        mock_post.side_effect = requests.ReadTimeout('sentinel-text-4243')
        session, _g, _m = self.session()
        with self.assertLogs(CHANNEL_LOGGER, level='WARNING') as logs, mute_logger(JOB_LOGGER):
            run_ai_job(self.env, session)
        lines = '\n'.join(logs.output)
        self.assertNotIn('File "', lines)
        self.assertNotIn('sentinel-text-4243', lines)
