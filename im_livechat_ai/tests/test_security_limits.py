# -*- coding: utf-8 -*-
"""Link allowlist cannot be bypassed with backslash/userinfo; active AI jobs
per live chat channel are bounded."""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields

from odoo.tests.common import TransactionCase

from odoo.addons.im_livechat_ai.models.discuss_channel import (
    MAX_MESSAGE_CHARS, SKIP_CONTEXT_KEY, _truncate_text)
from odoo.addons.im_livechat_ai.tools.markdown import markdown_to_html, safe_url

from .common import TEST_BACKEND, livechat_operator
from .test_ai_job import POST, _ok

HOSTS = {'store.example'}
THREAD = 'odoo.addons.im_livechat_ai.models.livechat_ai_job.threading.Thread'


class TestLinkAllowlistBypass(TransactionCase):

    def render(self, text):
        return str(markdown_to_html(text, link_hosts=HOSTS))

    def test_backslash_userinfo_is_not_a_link(self):
        for url in (r'https://evil.example\@store.example/x',
                    r'https://store.example\.evil.example/x',
                    'https://store.example@evil.example/x',
                    'https://user:pw@store.example/x'):
            html = self.render('[open](%s)' % url)
            self.assertNotIn('<a ', html, url)
            self.assertNotIn('evil.example', html.split('open')[0], url)

    def test_normal_allowed_link_still_works(self):
        html = self.render('[shop](https://store.example/shop/a?x=1&y=a@b)')
        self.assertIn('<a href="https://store.example/shop/a?x=1&amp;y=a@b"', html)

    def test_safe_url_rejects_backslash_and_userinfo(self):
        self.assertIsNone(safe_url(r'https://a.example\@b.example/'))
        self.assertIsNone(safe_url('https://a@b.example/'))
        self.assertEqual(safe_url('https://b.example/p@q'), 'https://b.example/p@q')


class TestActiveJobCap(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND, 'name': 'Cap Channel', 'ai_enabled': True,
            'ai_model': 'chatgpt-pool/gpt-6-sol',
        })
        cls.operator = livechat_operator(cls.env)
        cls.visitor = cls.env['res.partner'].create({'name': 'Cap Visitor'})
        cls.Job = cls.env['im_livechat.ai.job']

    def test_reply_jobs_stop_at_the_cap(self):
        self.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '2')
        with patch(THREAD):
            made = []
            for i in range(4):
                session = self.env['discuss.channel'].create({
                    'name': 'Cap %s' % i, 'channel_type': 'livechat',
                    'livechat_operator_id': self.operator.id,
                    'livechat_channel_id': self.channel.id,
                })
                message = session.message_post(
                    body='hello %s' % i, author_id=self.visitor.id,
                    message_type='comment', subtype_xmlid='mail.mt_comment')
                made.append(bool(self.Job.search([('message_id', '=', message.id)])))
        self.assertEqual(made.count(True), 2)

    def test_limit_zero_disables_the_cap(self):
        self.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '0')
        self.assertFalse(self.Job._active_jobs_over_limit(self.channel))

    def _session(self, name):
        return self.env['discuss.channel'].create({
            'name': name, 'channel_type': 'livechat',
            'livechat_operator_id': self.operator.id,
            'livechat_channel_id': self.channel.id,
        })

    def _visitor_post(self, session, body):
        return session.message_post(
            body=body, author_id=self.visitor.id,
            message_type='comment', subtype_xmlid='mail.mt_comment')

    def test_deferred_jobs_do_not_starve_other_sessions(self):
        """Messages written during a handoff wait queue deferred jobs; they
        must not use up the live reply quota of the whole channel."""
        self.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '2')
        with patch(THREAD):
            waiting = self._session('Waiting session')
            waiting._ai_pause(minutes=10, reason='handoff')
            for i in range(4):
                self._visitor_post(waiting, 'still waiting %s' % i)
            deferred = self.Job.search([
                ('discuss_channel_id', '=', waiting.id), ('deferred', '=', True)])
            self.assertTrue(deferred)
            self.assertFalse(self.Job._active_jobs_over_limit(self.channel))
            other = self._session('Other session')
            message = self._visitor_post(other, 'hello there')
        self.assertTrue(self.Job.search([
            ('message_id', '=', message.id), ('kind', '=', 'reply')]))

    def test_live_reply_jobs_still_count_with_deferred_present(self):
        """Positive control: live reply jobs still reach the cap."""
        self.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '2')
        with patch(THREAD):
            waiting = self._session('Waiting session')
            waiting._ai_pause(minutes=10, reason='handoff')
            self._visitor_post(waiting, 'still waiting')
            made = []
            for i in range(4):
                message = self._visitor_post(self._session('Live %s' % i), 'hello %s' % i)
                made.append(bool(self.Job.search([('message_id', '=', message.id)])))
        self.assertEqual(made.count(True), 2)

    def test_one_visitor_many_sessions_stays_within_channel_cap(self):
        """Evidence for the visitor-triggered load finding: however many
        sessions one visitor opens, the unfinished reply jobs of the channel
        never exceed the cap."""
        self.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '3')
        with patch(THREAD):
            for i in range(10):
                self._visitor_post(self._session('Flood %s' % i), 'flood %s' % i)
        active = self.Job.search_count([
            ('livechat_channel_id', '=', self.channel.id),
            ('kind', '=', 'reply'), ('state', 'in', ('pending', 'running'))])
        self.assertEqual(active, 3)


class TestDeferredJobCap(TransactionCase):
    """Deferred (handoff wait) jobs are released by the recovery cron: those
    being run count towards the channel cap, and the cron does not start more
    of them than the cap allows (the rest stay pending for a later run)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND, 'name': 'Deferred Cap Channel', 'ai_enabled': True,
        })
        cls.operator = livechat_operator(cls.env)
        cls.visitor = cls.env['res.partner'].create({'name': 'Deferred Cap Visitor'})
        cls.Job = cls.env['im_livechat.ai.job']
        cls.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '2')

    def _due_deferred_job(self, name, state='pending', started_ago=None):
        session = self.env['discuss.channel'].create({
            'name': name, 'channel_type': 'livechat',
            'livechat_operator_id': self.operator.id,
            'livechat_channel_id': self.channel.id,
        })
        # SKIP_CONTEXT_KEY: the post itself queues no live job.
        message = session.with_context(**{SKIP_CONTEXT_KEY: True}).message_post(
            body='waiting %s' % name, author_id=self.visitor.id,
            message_type='comment', subtype_xmlid='mail.mt_comment')
        now = fields.Datetime.now()
        return self.Job.sudo().create({
            'discuss_channel_id': session.id,
            'livechat_channel_id': self.channel.id,
            'message_id': message.id,
            'kind': 'reply',
            'deferred': True,
            'run_after': now - timedelta(minutes=1),
            'state': state,
            'started_at': (now - (started_ago or timedelta())) if state == 'running' else False,
        })

    def test_running_deferred_jobs_count_towards_the_cap(self):
        self._due_deferred_job('Running 1', state='running')
        self.assertFalse(self.Job._active_jobs_over_limit(self.channel))
        self._due_deferred_job('Running 2', state='running')
        self.assertTrue(self.Job._active_jobs_over_limit(self.channel))

    def _age_created(self, job, minutes):
        """Make the job look created `minutes` ago (a long handoff wait)."""
        self.env.cr.execute(
            "UPDATE im_livechat_ai_job SET create_date = create_date - %s * interval '1 minute' WHERE id = %s",
            (minutes, job.id))
        job.invalidate_recordset(['create_date'])

    def test_long_waiting_running_deferred_jobs_count_towards_the_cap(self):
        """A deferred job created long before it became due still counts
        while it runs (its age is measured from its due time)."""
        for i in range(2):
            self._age_created(self._due_deferred_job('Long wait %s' % i, state='running'), 45)
        self.assertTrue(self.Job._active_jobs_over_limit(self.channel))

    @patch(POST)
    def test_due_job_waits_behind_long_waiting_running_jobs(self, mock_post):
        mock_post.return_value = _ok()
        for i in range(2):
            self._age_created(self._due_deferred_job('Long wait %s' % i, state='running'), 45)
        waiting = self._due_deferred_job('Waiting')
        self._age_created(waiting, 45)
        waiting._run_job(commit=False)
        self.assertEqual(waiting.state, 'pending')
        self.assertEqual(waiting.attempts, 0)
        mock_post.assert_not_called()

    def test_long_gone_running_deferred_job_does_not_count(self):
        """Positive control: a deferred job whose due time is also long past
        is given up by the age rule and is not counted."""
        for i in range(2):
            job = self._due_deferred_job('Gone %s' % i, state='running')
            self._age_created(job, 90)
            job.write({'run_after': fields.Datetime.now() - timedelta(minutes=45)})
        self.assertFalse(self.Job._active_jobs_over_limit(self.channel))

    def test_pending_deferred_jobs_do_not_count(self):
        for i in range(3):
            self._due_deferred_job('Pending %s' % i)
        self.assertFalse(self.Job._active_jobs_over_limit(self.channel))

    @patch(POST)
    def test_due_job_waits_while_the_channel_is_full(self, mock_post):
        mock_post.return_value = _ok()
        self._due_deferred_job('Busy 1', state='running')
        self._due_deferred_job('Busy 2', state='running')
        waiting = self._due_deferred_job('Waiting')
        waiting._run_job(commit=False)
        self.assertEqual(waiting.state, 'pending')
        self.assertEqual(waiting.attempts, 0)
        mock_post.assert_not_called()

    @patch(POST)
    def test_cron_starts_no_more_deferred_jobs_than_the_cap(self, mock_post):
        mock_post.return_value = _ok()
        jobs = [self._due_deferred_job('Session %s' % i) for i in range(5)]
        self.Job._cron_recover_jobs(commit=False)
        started = [j for j in jobs if j.attempts]
        self.assertEqual(len(started), 2)
        self.assertEqual(mock_post.call_count, 2)
        for job in jobs:
            if not job.attempts:
                self.assertEqual(job.state, 'pending')

    @patch(POST)
    def test_waiting_jobs_run_once_there_is_room(self, mock_post):
        """Positive control: nothing is dropped, later cron runs finish them."""
        mock_post.return_value = _ok()
        jobs = [self._due_deferred_job('Session %s' % i) for i in range(5)]
        for _i in range(4):
            self.Job._cron_recover_jobs(commit=False)
        self.assertEqual({j.state for j in jobs}, {'done'})
        self.assertEqual(mock_post.call_count, 5)

    @patch(POST)
    def test_one_session_is_not_blocked_by_the_cap_of_another_channel(self, mock_post):
        mock_post.return_value = _ok()
        other = self.env['im_livechat.channel'].create({
            **TEST_BACKEND, 'name': 'Other Channel', 'ai_enabled': True})
        self._due_deferred_job('Busy 1', state='running')
        self._due_deferred_job('Busy 2', state='running')
        job = self._due_deferred_job('Elsewhere')
        job.sudo().write({'livechat_channel_id': other.id})
        job.discuss_channel_id.sudo().write({'livechat_channel_id': other.id})
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')


    @patch(POST)
    def test_recovered_running_deferred_job_is_not_blocked_by_itself(self, mock_post):
        """A deferred job whose process died (state running) already holds a
        slot: with a limit of 1 the recovery cron must still finish it."""
        mock_post.return_value = _ok()
        self.env['ir.config_parameter'].sudo().set_param('im_livechat_ai.max_active_jobs', '1')
        job = self._due_deferred_job('Stale', state='running', started_ago=timedelta(minutes=5))
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(job.state, 'done')

    @patch(POST)
    def test_all_recovered_running_deferred_jobs_finish_at_the_limit(self, mock_post):
        mock_post.return_value = _ok()
        jobs = [self._due_deferred_job('Stale %s' % i, state='running',
                                       started_ago=timedelta(minutes=5)) for i in range(2)]
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual({j.state for j in jobs}, {'done'})

    @patch(POST)
    def test_obsolete_due_job_is_closed_even_when_the_channel_is_full(self, mock_post):
        """A deferred job that will not call the backend (a newer job covers
        it, the AI is off...) is closed at once instead of waiting for room."""
        mock_post.return_value = _ok()
        self._due_deferred_job('Busy 1', state='running')
        self._due_deferred_job('Busy 2', state='running')
        old = self._due_deferred_job('Superseded')
        newer = self.Job.sudo().create({
            'discuss_channel_id': old.discuss_channel_id.id,
            'livechat_channel_id': self.channel.id,
            'message_id': old.message_id.id,
            'kind': 'reply', 'deferred': True,
            'run_after': fields.Datetime.now() + timedelta(minutes=5),
        })
        old._run_job(commit=False)
        self.assertEqual(old.state, 'skipped')
        self.assertEqual(newer.state, 'pending')
        mock_post.assert_not_called()

    @patch(POST)
    def test_cron_closes_obsolete_due_jobs_of_a_full_channel(self, mock_post):
        mock_post.return_value = _ok()
        self._due_deferred_job('Busy 1', state='running')
        self._due_deferred_job('Busy 2', state='running')
        old = self._due_deferred_job('Superseded')
        self.Job.sudo().create({
            'discuss_channel_id': old.discuss_channel_id.id,
            'livechat_channel_id': self.channel.id,
            'message_id': old.message_id.id,
            'kind': 'reply', 'deferred': True,
            'run_after': fields.Datetime.now() + timedelta(minutes=5),
        })
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(old.state, 'skipped')

    def test_new_deferred_job_supersedes_pending_ones_of_the_session(self):
        """Every message of a handoff wait queues a deferred job; only the
        newest one answers, so a session keeps at most one pending."""
        session = self.env['discuss.channel'].create({
            'name': 'Waiting', 'channel_type': 'livechat',
            'livechat_operator_id': self.operator.id,
            'livechat_channel_id': self.channel.id,
        })
        session._ai_pause(minutes=10, reason='handoff')
        with patch(THREAD):
            for i in range(5):
                session.message_post(
                    body='still waiting %s' % i, author_id=self.visitor.id,
                    message_type='comment', subtype_xmlid='mail.mt_comment')
        deferred = self.Job.search([('discuss_channel_id', '=', session.id), ('deferred', '=', True)],
                                   order='id')
        self.assertEqual(len(deferred), 5)
        self.assertEqual(deferred.mapped('state'), ['skipped'] * 4 + ['pending'])

    def test_deferred_jobs_of_other_sessions_are_not_superseded(self):
        jobs = [self._due_deferred_job('Session %s' % i) for i in range(2)]
        with patch(THREAD):
            self.Job._create_for_message(
                jobs[0].discuss_channel_id, jobs[0].message_id,
                run_after=fields.Datetime.now() + timedelta(minutes=5))
        self.assertEqual(jobs[0].state, 'skipped')
        self.assertEqual(jobs[1].state, 'pending')


class TestLongVisitorMessage(TransactionCase):

    def test_long_visitor_message_is_truncated_for_the_backend(self):
        channel = self.env['im_livechat.channel'].create({
            **TEST_BACKEND, 'name': 'Long Message Channel', 'ai_enabled': True})
        operator = livechat_operator(self.env)
        visitor = self.env['res.partner'].create({'name': 'Long Visitor'})
        session = self.env['discuss.channel'].create({
            'name': 'Long', 'channel_type': 'livechat',
            'livechat_operator_id': operator.id, 'livechat_channel_id': channel.id,
        })
        limit = MAX_MESSAGE_CHARS
        post = session.with_context(**{SKIP_CONTEXT_KEY: True}).message_post
        post(body='x' * (limit * 50), author_id=visitor.id,
             message_type='comment', subtype_xmlid='mail.mt_comment')
        post(body='short question', author_id=visitor.id,
             message_type='comment', subtype_xmlid='mail.mt_comment')
        history = session._ai_conversation(channel)['history']
        self.assertEqual(len(history), 2)
        self.assertLessEqual(len(history[0]['text']), limit + 1)
        self.assertEqual(history[1]['text'], 'short question')

    def test_message_at_the_limit_is_kept_whole(self):
        self.assertEqual(_truncate_text('y' * MAX_MESSAGE_CHARS), 'y' * MAX_MESSAGE_CHARS)
