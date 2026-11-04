# -*- coding: utf-8 -*-
"""Link allowlist cannot be bypassed with backslash/userinfo; active AI jobs
per live chat channel are bounded."""

from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.im_livechat_ai.tools.markdown import markdown_to_html, safe_url

from .common import TEST_BACKEND, livechat_operator

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
