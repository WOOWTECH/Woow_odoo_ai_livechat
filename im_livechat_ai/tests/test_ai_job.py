# -*- coding: utf-8 -*-
"""Package C (P1-4..P1-7): durable AI reply jobs.

All HTTP is mocked; no real key is used. Jobs run synchronously in the test
transaction with _run_job(commit=False) / _cron_recover_jobs(commit=False).
"""

from contextlib import closing
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError
from odoo.sql_db import db_connect
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import html2plaintext, mute_logger

from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    LLM_REQUEST_TIMEOUT,
)
from odoo.addons.im_livechat_ai.models.livechat_ai_job import (
    CRON_TRIGGER_DELAY_SECONDS,
    DEBOUNCE_SECONDS,
    LOCK_NAMESPACE,
    MAX_JOB_RUNS,
    STALE_PENDING_SECONDS,
)

from .common import livechat_operator, setup_litellm

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
SLEEP = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep'
THREAD = 'odoo.addons.im_livechat_ai.models.livechat_ai_job.threading.Thread'
ERROR_TEXT = 'AI 暫時無法回覆，請留下聯絡方式。'


def _ok(content='您好，這是 AI 回覆'):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': {'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
    }
    response.raise_for_status = MagicMock()
    return response


class AiJobCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Job Channel',
            'ai_enabled': True,
            'ai_model': 'chatgpt-pool/gpt-6-sol',
            'ai_max_retries': 2,
            'ai_error_message': ERROR_TEXT,
        })
        cls.bot = cls.channel._get_or_create_bot_partner()
        cls.operator = livechat_operator(cls.env)
        cls.visitor = cls.env['res.partner'].create({'name': 'Job Visitor'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Job Session',
            'channel_type': 'livechat',
            'livechat_operator_id': cls.operator.id,
            'livechat_channel_id': cls.channel.id,
        })
        cls.Job = cls.env['im_livechat.ai.job']

    def _visitor_post(self, body):
        return self.session.message_post(
            body=body,
            author_id=self.visitor.id,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
        )

    def _jobs(self, session=None):
        return self.Job.search([('discuss_channel_id', '=', (session or self.session).id)], order='id')

    def _bot_messages(self):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', self.session.id),
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

    def _make_stale(self, job, state='running', attempts=1):
        """Simulate a run that died: committed claim, no lock holder."""
        past = fields.Datetime.now() - timedelta(minutes=5)
        job.write({'state': state, 'attempts': attempts, 'started_at': past, 'run_after': past})


class TestJobCreation(AiJobCase):

    def test_job_created_in_same_transaction_as_message(self):
        before = fields.Datetime.now()
        msg = self._visitor_post('請問營業時間？')
        job = self._jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.message_id, msg)
        self.assertEqual(job.livechat_channel_id, self.channel)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.attempts, 0)
        self.assertGreaterEqual(job.run_after, before + timedelta(seconds=int(DEBOUNCE_SECONDS)))
        # Processing starts only after commit: a post-commit callback, no thread yet.
        callbacks = [f for f in self.env.cr.postcommit._funcs
                     if getattr(f, 'im_livechat_ai_job_ids', None) == job.ids]
        self.assertEqual(len(callbacks), 1)

    def test_job_rolled_back_with_message(self):
        ids = {}
        with self.assertRaises(ZeroDivisionError):
            with self.env.cr.savepoint():
                ids['msg'] = self._visitor_post('這則會被回滾').id
                ids['job'] = self._jobs().id
                self.assertTrue(ids['job'])
                1 / 0  # noqa: B018 - abort the visitor transaction
        self.env.cr.execute("SELECT count(*) FROM im_livechat_ai_job WHERE id = %s", (ids['job'],))
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.env.cr.execute("SELECT count(*) FROM mail_message WHERE id = %s", (ids['msg'],))
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        # A leftover post-commit callback for the rolled-back job is harmless.
        self.assertEqual(self.Job.browse(ids['job'])._run_job(commit=False), (None, False))

    def test_no_job_for_bot_or_operator_messages(self):
        self.channel._create_bot_message(self.env, self.session, '機器人訊息')
        self.session.message_post(body='客服訊息', author_id=self.operator.id,
                                  message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertFalse(self._jobs())

    def test_job_creation_failure_does_not_block_visitor(self):
        with patch.object(type(self.Job), '_create_for_message', side_effect=RuntimeError('boom')), \
                mute_logger('odoo.addons.im_livechat_ai.models.discuss_channel'):
            msg = self._visitor_post('仍然要送出')
        self.assertTrue(msg.exists())
        self.assertFalse(self._jobs())

    @patch(THREAD)
    def test_postcommit_starts_non_daemon_thread(self, mock_thread):
        """Not a daemon: a recycling prefork worker (sys.exit) finishes it."""
        self._visitor_post('hi')
        job = self._jobs()
        mock_thread.assert_not_called()
        callback = next(f for f in self.env.cr.postcommit._funcs
                        if getattr(f, 'im_livechat_ai_job_ids', None) == job.ids)
        callback()
        mock_thread.assert_called_once()
        self.assertIs(mock_thread.call_args.kwargs['daemon'], False)
        self.assertEqual(mock_thread.call_args.kwargs['args'], (self.env.cr.dbname, job.id))


class TestJobProcessing(AiJobCase):

    @patch(POST)
    def test_single_message_answered(self, mock_post):
        mock_post.return_value = _ok('營業時間是 9:00-18:00')
        self._visitor_post('請問營業時間？')
        job = self._jobs()
        self.assertEqual(job._run_job(commit=False), (self.session.id, True))
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.llm_calls, 1)
        self.assertTrue(job.finished_at)
        self.assertIn('營業時間是 9:00-18:00', job.reply_message_id.body)
        self.assertEqual(len(self._bot_messages()), 1)
        # Running the finished job again does nothing.
        job._run_job(commit=False)
        self.assertEqual(mock_post.call_count, 1)

    @patch(POST)
    def test_three_rapid_messages_one_reply(self, mock_post):
        mock_post.return_value = _ok('一次回覆三個問題')
        for body in ('第一個問題', '第二個問題', '第三個問題'):
            self._visitor_post(body)
        jobs = self._jobs()
        self.assertEqual(len(jobs), 3)
        # Each job's thread wakes after the debounce, oldest first.
        for job in jobs:
            job._run_job(commit=False)
        self.assertEqual(jobs.mapped('state'), ['skipped', 'skipped', 'done'])
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(len(self._bot_messages()), 1)
        sent = [m['content'] for m in mock_post.call_args.kwargs['json']['messages']]
        for body in ('第一個問題', '第二個問題', '第三個問題'):
            self.assertIn(body, sent)

    @patch(POST)
    def test_chain_answers_newest_and_supersedes_older(self, mock_post):
        """The lock holder runs the newest pending job; older ones are closed."""
        mock_post.return_value = _ok('合併回覆')
        for body in ('a', 'b', 'c'):
            self._visitor_post(body)
        jobs = self._jobs()
        self.Job._run_chain_in_transaction(jobs[0].id)
        self.assertEqual(jobs.mapped('state'), ['skipped', 'skipped', 'done'])
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(len(self._bot_messages()), 1)

    @patch(POST)
    def test_stale_job_skipped_before_posting(self, mock_post):
        """A message arriving during the LLM call: the old answer is dropped
        and the newest job answers with both messages as context."""
        def answer_while_visitor_types(*args, **kwargs):
            if mock_post.call_count == 1:
                self._visitor_post('還有一個問題')
            return _ok(f'回覆 {mock_post.call_count}')

        mock_post.side_effect = answer_while_visitor_types
        self._visitor_post('第一個問題')
        first = self._jobs()
        first._run_job(commit=False)
        self.assertEqual(first.state, 'skipped')
        self.assertFalse(self._bot_messages())
        second = self._jobs() - first
        self.assertEqual(second.state, 'pending')
        second._run_job(commit=False)
        self.assertEqual(second.state, 'done')
        self.assertEqual(len(self._bot_messages()), 1)
        sent = [m['content'] for m in mock_post.call_args.kwargs['json']['messages']]
        self.assertIn('第一個問題', sent)
        self.assertIn('還有一個問題', sent)

    @patch(POST)
    def test_lock_prevents_concurrent_processing(self, mock_post):
        """Another process (separate connection) holds the session lock."""
        mock_post.return_value = _ok()
        self._visitor_post('hi')
        job = self._jobs()
        with closing(db_connect(self.env.cr.dbname).cursor()) as other:
            other.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, self.session.id))
            self.assertTrue(other.fetchone()[0])
            try:
                self.assertEqual(job._run_job(commit=False), (self.session.id, False))
                self.assertEqual(job.state, 'pending')
                self.assertEqual(job.attempts, 0)
                # The cron leaves a locked session alone, even for a stale job.
                self._make_stale(job)
                self.Job._cron_recover_jobs(commit=False)
                self.assertEqual(job.state, 'running')
                self.assertEqual(job.attempts, 1)
                mock_post.assert_not_called()
            finally:
                other.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, self.session.id))
        # Lock released: the next attempt processes it.
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(mock_post.call_count, 1)

    @patch(POST)
    def test_other_sessions_not_blocked(self, mock_post):
        mock_post.return_value = _ok()
        other_session = self.env['discuss.channel'].create({
            'name': 'Other Session',
            'channel_type': 'livechat',
            'livechat_operator_id': self.operator.id,
            'livechat_channel_id': self.channel.id,
        })
        other_session.message_post(body='hi', author_id=self.visitor.id,
                                   message_type='comment', subtype_xmlid='mail.mt_comment')
        job = self._jobs(other_session)
        with closing(db_connect(self.env.cr.dbname).cursor()) as other:
            other.execute("SELECT pg_advisory_lock(%s, %s)", (LOCK_NAMESPACE, self.session.id))
            try:
                self.assertEqual(job._run_job(commit=False), (other_session.id, True))
            finally:
                other.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, self.session.id))
        self.assertEqual(job.state, 'done')

    @patch(POST)
    def test_disabled_after_queue_skips(self, mock_post):
        self._visitor_post('hi')
        job = self._jobs()
        self.channel.write({'ai_enabled': False})
        job._run_job(commit=False)
        self.assertEqual(job.state, 'skipped')
        mock_post.assert_not_called()


class TestJobRecovery(AiJobCase):

    @patch(POST)
    def test_cron_recovers_job_after_crash(self, mock_post):
        mock_post.return_value = _ok('重啟後補回覆')
        self._visitor_post('重啟前的問題')
        job = self._jobs()
        self._make_stale(job, state='running', attempts=1)
        self.assertEqual(self.Job._cron_recover_jobs(commit=False), 1)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.attempts, 2)
        self.assertIn('重啟後補回覆', job.reply_message_id.body)

    @patch(POST)
    def test_cron_recovers_lost_pending_job(self, mock_post):
        mock_post.return_value = _ok('補回覆')
        self._visitor_post('執行緒遺失')
        job = self._jobs()
        self._make_stale(job, state='pending', attempts=0)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.attempts, 1)

    @patch(THREAD)
    def test_cron_hands_each_session_to_one_thread(self, mock_thread):
        """Real mode: the cron returns at once; a job can outlast the cron
        watchdog, so each session's stale jobs go to one recovery thread."""
        self._visitor_post('a')
        self._visitor_post('b')
        jobs = self._jobs()
        for job in jobs:
            self._make_stale(job, state='pending', attempts=0)
        self.assertEqual(self.Job._cron_recover_jobs(), 2)
        mock_thread.assert_called_once()
        self.assertIs(mock_thread.call_args.kwargs['daemon'], False)
        self.assertEqual(mock_thread.call_args.kwargs['args'], (self.env.cr.dbname, jobs.ids))
        self.assertEqual(jobs.mapped('state'), ['pending', 'pending'])

    @patch(POST)
    def test_cron_ignores_fresh_jobs(self, mock_post):
        self._visitor_post('剛送出')
        job = self._jobs()
        self.assertEqual(self.Job._cron_recover_jobs(commit=False), 0)
        self.assertEqual(job.state, 'pending')
        mock_post.assert_not_called()

    def _answered_by(self, job, context_message, reply_body='已經回覆過了'):
        """Mark `job` as a finished reply whose LLM context ended at
        `context_message` (the reply itself is posted now, i.e. last)."""
        reply = self.channel._create_bot_message(self.env, self.session, reply_body)
        job.write({'state': 'done', 'reply_message_id': reply.id,
                   'context_message_id': context_message.id})
        return reply

    @patch(POST)
    def test_cron_does_not_double_answer(self, mock_post):
        """A later job's reply had this trigger message in its context."""
        self._visitor_post('問題')
        second = self._visitor_post('補充')
        job_a, job_b = self._jobs()
        self._answered_by(job_a, second)
        self._make_stale(job_b, state='running', attempts=1)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job_b.state, 'done')
        self.assertIn('一併回答', job_b.error)
        mock_post.assert_not_called()
        self.assertEqual(len(self._bot_messages()), 1)

    @patch(POST)
    def test_message_posted_during_reply_is_still_answered(self, mock_post):
        """Race: B was posted while A's reply was written, so A's reply comes
        after B but B was not in A's LLM context: B must still be answered."""
        mock_post.return_value = _ok('回答 B')
        message_a = self._visitor_post('問題 A')
        job_a = self._jobs()
        message_b = self._visitor_post('問題 B')
        job_b = self._jobs() - job_a
        reply_a = self._answered_by(job_a, message_a, '回答 A')
        self.assertGreater(reply_a.id, message_b.id)
        job_b._run_job(commit=False)
        self.assertEqual(job_b.state, 'done')
        self.assertEqual(mock_post.call_count, 1)
        self.assertIn('回答 B', job_b.reply_message_id.body)
        self.assertEqual(job_b.context_message_id, reply_a.id)

    @patch(POST)
    def test_handoff_posted_during_reply_still_gets_notice(self, mock_post):
        """A handoff job is never closed because a bot message came after it."""
        message_a = self._visitor_post('問題 A')
        job_a = self._jobs()
        message_b = self.session.message_post(
            body='找真人', author_id=self.visitor.id, message_type='comment',
            subtype_xmlid='mail.mt_comment')
        job_b = self._jobs() - job_a
        self.assertEqual(job_b.kind, 'handoff')
        self._answered_by(job_a, message_a, '回答 A')
        job_b._run_job(commit=False)
        self.assertEqual(job_b.state, 'done')
        self.assertIn(self.channel.ai_handoff_message, html2plaintext(job_b.reply_message_id.body))
        self.assertTrue(self._odoobot_chat_messages(self.operator))
        self.assertGreater(job_b.reply_message_id.id, message_b.id)
        mock_post.assert_not_called()

    @patch(POST)
    def test_context_message_id_recorded(self, mock_post):
        mock_post.return_value = _ok()
        message = self._visitor_post('hi')
        job = self._jobs()
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.context_message_id, message.id)

    @patch(POST)
    def test_cron_respects_max_runs(self, mock_post):
        self._visitor_post('一直當機')
        job = self._jobs()
        self._make_stale(job, state='running', attempts=MAX_JOB_RUNS)
        self.Job._cron_recover_jobs(commit=False)
        mock_post.assert_not_called()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.attempts, MAX_JOB_RUNS)
        self.assertIn(ERROR_TEXT, job.reply_message_id.body)
        self.assertTrue(self._odoobot_chat_messages(self.operator))

    @patch(POST)
    def test_max_runs_superseded_job_is_closed_quietly(self, mock_post):
        mock_post.return_value = _ok('最新的回覆')
        self._visitor_post('一直當機')
        job = self._jobs()
        self._make_stale(job, state='running', attempts=MAX_JOB_RUNS)
        self._visitor_post('新問題')
        newer = self._jobs() - job
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job.state, 'skipped')
        self.assertEqual(newer.state, 'done')
        bodies = ' '.join(self._bot_messages().mapped('body'))
        self.assertNotIn(ERROR_TEXT, bodies)
        self.assertFalse(self._odoobot_chat_messages(self.operator))

    @patch(POST)
    def test_max_runs_paused_or_disabled_job_is_closed_quietly(self, mock_post):
        self._visitor_post('一直當機')
        job = self._jobs()
        self._make_stale(job, state='running', attempts=MAX_JOB_RUNS)
        self.session._ai_pause()
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(self._bot_messages())

        self.session.write({'ai_paused_until': False})
        self._visitor_post('再一次')
        job2 = self._jobs() - job
        self._make_stale(job2, state='running', attempts=MAX_JOB_RUNS)
        self.channel.write({'ai_enabled': False})
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job2.state, 'skipped')
        self.assertFalse(self._bot_messages())
        self.assertFalse(self._odoobot_chat_messages(self.operator))
        mock_post.assert_not_called()

    @patch(POST)
    def test_max_runs_handoff_posts_handoff_notice(self, mock_post):
        message = self._visitor_post('placeholder')
        job = self.Job._create_for_message(self.session, message, kind='handoff')
        self._jobs().filtered(lambda j: j.kind == 'reply').write({'state': 'skipped'})
        self._make_stale(job, state='running', attempts=MAX_JOB_RUNS)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job.state, 'done')
        body = html2plaintext(job.reply_message_id.body)
        self.assertIn(self.channel.ai_handoff_message, body)
        self.assertNotIn(ERROR_TEXT, body)
        self.assertTrue(self._odoobot_chat_messages(self.operator))
        mock_post.assert_not_called()

    @patch(THREAD)
    def test_cron_skips_locked_sessions_and_reports_no_progress(self, mock_thread):
        """A session being processed is not handed to a second thread, and the
        cron reports no remaining work (Odoo would re-run it at once)."""
        other_session = self.env['discuss.channel'].create({
            'name': 'Other Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.operator.id,
            'livechat_channel_id': self.channel.id,
        })
        self._visitor_post('a')
        other_session.message_post(body='b', author_id=self.visitor.id,
                                   message_type='comment', subtype_xmlid='mail.mt_comment')
        for job in self._jobs() | self._jobs(other_session):
            self._make_stale(job, state='running', attempts=1)
        IrCron = type(self.env['ir.cron'])
        with closing(db_connect(self.env.cr.dbname).cursor()) as other, \
                patch.object(IrCron, '_notify_progress') as notify:
            other.execute("SELECT pg_advisory_lock(%s, %s)", (LOCK_NAMESPACE, self.session.id))
            try:
                self.assertTrue(self.Job._session_lock_held(self.session.id))
                self.assertFalse(self.Job._session_lock_held(other_session.id))
                self.assertEqual(self.Job._cron_recover_jobs(), 1)
            finally:
                other.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, self.session.id))
        mock_thread.assert_called_once()
        self.assertEqual(mock_thread.call_args.kwargs['args'],
                         (self.env.cr.dbname, self._jobs(other_session).ids))
        notify.assert_not_called()

    def test_visitor_post_triggers_recovery_cron(self):
        """If the thread's process dies, the cron process picks the job up
        soon after it is overdue instead of waiting for the interval."""
        cron = self.env.ref('im_livechat_ai.ir_cron_recover_ai_jobs')
        Trigger = self.env['ir.cron.trigger'].sudo()
        before = Trigger.search([('cron_id', '=', cron.id)])
        self._visitor_post('hi')
        job = self._jobs()
        triggers = Trigger.search([('cron_id', '=', cron.id)]) - before
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers.call_at, job.run_after + timedelta(seconds=CRON_TRIGGER_DELAY_SECONDS))
        self.assertGreater(CRON_TRIGGER_DELAY_SECONDS, STALE_PENDING_SECONDS)

    @patch(POST)
    def test_final_checks_run_with_session_row_locked(self, mock_post):
        """The session row is locked after the LLM call and before the last
        newer-job / pause checks; the lock timeout is reset afterwards."""
        mock_post.return_value = _ok()
        self._visitor_post('hi')
        job = self._jobs()
        JobModel = type(self.Job)
        events = []
        real_lock, real_newer = JobModel._lock_session_row, JobModel._has_newer_job

        def lock(rec):
            events.append('lock')
            return real_lock(rec)

        def newer(rec):
            events.append('newer')
            return real_newer(rec)

        def post(*args, **kwargs):
            events.append('http')
            return _ok()

        mock_post.side_effect = post
        with patch.object(JobModel, '_lock_session_row', lock), \
                patch.object(JobModel, '_has_newer_job', newer):
            job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(events, ['newer', 'http', 'lock', 'newer'])
        self.env.cr.execute("SHOW lock_timeout")
        self.assertEqual(self.env.cr.fetchone()[0], '0')

    @patch(POST)
    def test_cron_gives_up_on_old_jobs(self, mock_post):
        self._visitor_post('一小時前的問題')
        job = self._jobs()
        self._make_stale(job, state='pending', attempts=0)
        self.env.cr.execute(
            "UPDATE im_livechat_ai_job SET create_date = create_date - interval '1 hour' WHERE id = %s",
            (job.id,))
        self.env.invalidate_all()
        self.Job._cron_recover_jobs(commit=False)
        mock_post.assert_not_called()
        self.assertEqual(job.state, 'failed')
        self.assertFalse(self._bot_messages())  # visitor is likely gone
        self.assertTrue(self._odoobot_chat_messages(self.operator))

    def test_cron_record(self):
        cron = self.env.ref('im_livechat_ai.ir_cron_recover_ai_jobs')
        self.assertTrue(cron.active)
        self.assertEqual((cron.interval_number, cron.interval_type), (2, 'minutes'))
        self.assertEqual(cron.code.strip(), 'model._cron_recover_jobs()')

    def test_cleanup_old_jobs(self):
        self._visitor_post('舊的')
        old = self._jobs()
        old.write({'state': 'done'})
        self.env.cr.execute(
            "UPDATE im_livechat_ai_job SET create_date = now() - interval '30 days' WHERE id = %s", (old.id,))
        self._visitor_post('新的')
        new = self._jobs() - old
        self.env.invalidate_all()
        self.env['llm.api.log']._cleanup_old_logs()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class TestJobFailure(AiJobCase):

    @patch(SLEEP)
    @patch(POST)
    def test_failure_posts_error_and_notifies_operators(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.ConnectionError('refused')
        ResPartner = type(self.env['res.partner'])
        self._visitor_post('請問價格？')
        job = self._jobs()
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send:
            job._run_job(commit=False)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.llm_calls, 3)
        self.assertIn('無法連線到 LiteLLM', job.error)
        # Visitor gets the channel's Chinese error message.
        self.assertEqual(len(self._bot_messages()), 1)
        self.assertIn(ERROR_TEXT, self._bot_messages().body)
        # Operator gets a persistent OdooBot chat message ...
        notes = self._odoobot_chat_messages(self.operator)
        self.assertEqual(len(notes), 1)
        self.assertIn('AI 客服無法回覆', notes.body)
        self.assertIn(f'discuss.channel_{self.session.id}', notes.body)
        # ... and a sticky toast through the bus.
        calls = [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']
        self.assertEqual(len(calls), 1)
        self.assertIn(self.operator, calls[0].args[0])
        self.assertTrue(calls[0].args[2]['sticky'])
        self.assertEqual(calls[0].args[2]['type'], 'danger')

    @patch(SLEEP)
    @patch(POST)
    def test_default_error_message_is_chinese(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.ConnectionError('refused')
        self.channel.write({'ai_error_message': False})
        self._visitor_post('hi')
        self._jobs()._run_job(commit=False)
        self.assertIn('AI 客服暫時無法回覆', self._bot_messages().body)

    @patch(SLEEP)
    @patch(POST)
    def test_notification_falls_back_to_channel_operators(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.ConnectionError('refused')
        lc_operator = new_test_user(self.env, login='job_lc_operator', name='Channel Operator',
                                    groups='base.group_user,im_livechat.im_livechat_group_user')
        self.channel.write({'user_ids': [(6, 0, lc_operator.ids)]})
        # Session whose operator is the bot itself (no human in the session).
        session = self.env['discuss.channel'].create({
            'name': 'Bot-only Session',
            'channel_type': 'livechat',
            'livechat_operator_id': self.bot.id,
            'livechat_channel_id': self.channel.id,
        })
        session.channel_member_ids.filtered(lambda m: m.partner_id != self.bot).unlink()
        session.message_post(body='hi', author_id=self.visitor.id,
                             message_type='comment', subtype_xmlid='mail.mt_comment')
        self._jobs(session)._run_job(commit=False)
        self.assertTrue(self._odoobot_chat_messages(lc_operator.partner_id))


class TestTimeoutsAndRetries(AiJobCase):

    @patch(SLEEP)
    @patch(POST)
    def test_timeout_retries_and_backoff(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.Timeout('read timeout')
        self._visitor_post('逾時測試')
        job = self._jobs()
        job._run_job(commit=False)
        self.assertEqual(mock_post.call_count, 3)  # 1 call + 2 retries
        for call in mock_post.call_args_list:
            self.assertEqual(call.kwargs['timeout'], LLM_REQUEST_TIMEOUT)
        self.assertEqual(LLM_REQUEST_TIMEOUT, 45)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [3, 10])
        self.assertEqual(job.llm_calls, 3)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.state, 'failed')
        self.assertIn('連線逾時', job.error)
        logs = self.env['llm.api.log'].search([('discuss_channel_id', '=', self.session.id)], order='id')
        self.assertEqual(logs.mapped('status'), ['retry', 'retry', 'error'])

    @patch(SLEEP)
    @patch(POST)
    def test_retry_then_success(self, mock_post, mock_sleep):
        mock_post.side_effect = [requests.Timeout('slow'), _ok('第二次成功')]
        self._visitor_post('重試測試')
        job = self._jobs()
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.llm_calls, 2)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [3])

    @patch(SLEEP)
    @patch(POST)
    def test_zero_retries(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.Timeout('slow')
        self.channel.write({'ai_max_retries': 0})
        self._visitor_post('不重試')
        self._jobs()._run_job(commit=False)
        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @patch(SLEEP)
    @patch(POST)
    def test_legacy_retry_value_is_capped(self, mock_post, mock_sleep):
        """A value stored before the cap (old range 1-10) still makes at most 3 calls."""
        mock_post.side_effect = requests.Timeout('slow')
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_max_retries = 10 WHERE id = %s",
                            (self.channel.id,))
        self.env.invalidate_all()
        self._visitor_post('舊設定')
        self._jobs()._run_job(commit=False)
        self.assertEqual(mock_post.call_count, 3)


class TestJobAccess(AiJobCase):

    def test_manager_read_only_others_denied(self):
        self._visitor_post('hi')
        job = self._jobs()
        manager = new_test_user(self.env, login='job_manager', name='Job Manager',
                                groups='base.group_user,im_livechat.im_livechat_group_manager')
        lc_user = new_test_user(self.env, login='job_lc_user', name='Job LC User',
                                groups='base.group_user,im_livechat.im_livechat_group_user')
        self.assertEqual(job.with_user(manager).read(['state'])[0]['state'], 'pending')
        with self.assertRaises(AccessError):
            job.with_user(manager).write({'state': 'done'})
        with self.assertRaises(AccessError):
            self.Job.with_user(manager).create({'discuss_channel_id': self.session.id})
        with self.assertRaises(AccessError):
            job.with_user(lc_user).read(['state'])
