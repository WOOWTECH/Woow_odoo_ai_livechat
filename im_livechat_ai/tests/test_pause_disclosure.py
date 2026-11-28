# -*- coding: utf-8 -*-
"""Owner-approved round 2: explicit handoff keywords and the two pause kinds
with waiting notice and auto-resume (P0-1), range validation (P1-1), the
free-text model and no Settings page (18.0.1.2.0), job monitoring on the AI
tab (P1-6), AI disclosure (P1-7) and idempotent migration scripts (P2-4).

All HTTP is mocked; no real key is used. Jobs run synchronously in the test
transaction with _run_job(commit=False) / _cron_recover_jobs(commit=False).
"""

import base64
import importlib.util
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase, new_test_user
from odoo.tools import SQL, sql

from odoo.addons.im_livechat_ai.models.discuss_channel import RESUME_SYSTEM_NOTE
from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    HANDOFF_KEYWORDS_1_1_0,
    DEFAULT_HANDOFF_MESSAGE,
    DEFAULT_HANDOFF_PAUSE_MINUTES,
    DEFAULT_HANDOFF_WAIT_MINUTES,
    DEFAULT_PAUSE_NOTICE,
    DEFAULT_WELCOME_MESSAGE,
    LEGACY_HANDOFF_KEYWORDS,
)

from .common import TEST_BACKEND
from .common import make_ai_channel, sent_json, set_ai, split_time_line

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'


def _ok(content='AI 回覆'):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': {'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
    }
    response.raise_for_status = MagicMock()
    return response


def _load_migration(name):
    path = f"{get_module_path('im_livechat_ai')}/migrations/18.0.1.1.0/{name}.py"
    spec = importlib.util.spec_from_file_location(f'im_livechat_ai_r2_{name.replace("-", "_")}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PauseCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = make_ai_channel(cls.env, {
            **TEST_BACKEND,
            'name': 'R2 Channel',
            'ai_enabled': True,
            'ai_system_prompt': '你是客服助理。',
        })
        cls.bot = cls.channel._get_bot_partner()
        cls.operator_user = new_test_user(
            cls.env, login='r2_operator', name='林客服',
            groups='base.group_user,im_livechat.im_livechat_group_user',
        )
        cls.operator = cls.operator_user.partner_id
        cls.visitor = cls.env['res.partner'].create({'name': 'R2 Visitor'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'R2 Session',
            'channel_type': 'livechat',
            'livechat_operator_id': cls.operator.id,
            'livechat_channel_id': cls.channel.id,
        })
        cls.Job = cls.env['im_livechat.ai.job']

    def _post(self, body, author):
        return self.session.message_post(
            body=body, author_id=author.id, message_type='comment', subtype_xmlid='mail.mt_comment',
        )

    def _jobs(self, **filters):
        domain = [('discuss_channel_id', '=', self.session.id)]
        domain += [(k, '=', v) for k, v in filters.items()]
        return self.Job.search(domain, order='id')

    def _bot_bodies(self):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', self.session.id),
            ('author_id', '=', self.bot.id),
        ], order='id').mapped('body')

    def _expire_pause(self):
        self.session.sudo().ai_paused_until = fields.Datetime.now() - timedelta(seconds=5)

    def _make_due(self, jobs):
        jobs.sudo().write({'run_after': fields.Datetime.now() - timedelta(seconds=1)})


class TestHandoffKeywords(PauseCase):
    """P0-1: only explicit requests hand over; English phrases match whole words."""

    def test_new_default(self):
        self.assertEqual(self.channel.ai_handoff_keywords, HANDOFF_KEYWORDS_1_1_0)
        keywords = self.channel._ai_handoff_keyword_list()
        for removed in ('客服', '人工', 'agent', 'operator', 'human'):
            self.assertNotIn(removed, keywords)
        self.assertIn('talk to a human', keywords)

    def test_explicit_requests_match(self):
        hit = self.channel._ai_is_handoff_request
        for text in ('我要找真人', '請幫我轉人工', '可以聯絡客服嗎', '真人客服在嗎', '我要客服',
                     'Can I talk to a HUMAN?', 'talk   to\na human please', 'Live Agent',
                     'I need a real person.', 'human agent!'):
            self.assertTrue(hit(text), text)

    def test_single_words_no_longer_match(self):
        hit = self.channel._ai_is_handoff_request
        for text in ('客服電話幾號？', '人工智慧是什麼', '你是真人嗎', 'Are you an agent?',
                     'operator manual', 'human body', 'olive agents', 'talk to a humanoid',
                     'unreal personal', ''):
            self.assertFalse(hit(text), text)

    def test_visitor_question_about_customer_service_gets_ai_reply(self):
        self._post('請問客服電話幾號？', self.visitor)
        job = self._jobs()
        self.assertEqual(job.kind, 'reply')
        self.assertFalse(self.session.ai_paused_until)

    def test_custom_english_phrase(self):
        self.channel.ai_handoff_keywords = '主管, Speak To  Manager'
        hit = self.channel._ai_is_handoff_request
        self.assertTrue(hit('please speak to manager'))
        self.assertTrue(hit('SPEAK TO\tMANAGER'))
        self.assertFalse(hit('speak to managers'))
        self.assertTrue(hit('找主管'))


class TestOperatorPauseKind(PauseCase):
    """P0-1 (a): an operator's comment pauses for 30 minutes, reason operator."""

    def test_operator_pause_and_extension(self):
        before = fields.Datetime.now()
        self._post('我來處理', self.operator)
        self.assertEqual(self.session.ai_pause_reason, 'operator')
        first = self.session.ai_paused_until
        self.assertAlmostEqual(first, before + timedelta(minutes=DEFAULT_HANDOFF_PAUSE_MINUTES),
                               delta=timedelta(seconds=5))
        # Each operator comment extends it to now + 30 (never shortens).
        self.session.sudo().ai_paused_until = fields.Datetime.now() + timedelta(minutes=2)
        self._post('還在處理', self.operator)
        self.assertGreater(self.session.ai_paused_until, fields.Datetime.now() + timedelta(minutes=29))
        later = fields.Datetime.now() + timedelta(hours=3)
        self.session.sudo().ai_paused_until = later
        self._post('好的', self.operator)
        self.assertEqual(self.session.ai_paused_until, later)

    def test_no_reply_and_no_notice_during_operator_pause(self):
        self._post('我來處理', self.operator)
        self._post('謝謝', self.visitor)
        self._post('還有一個問題', self.visitor)
        self.assertFalse(self._jobs())
        self.assertFalse(self.session.ai_pause_notice_sent)


class TestHandoffWait(PauseCase):
    """P0-1 (b)(c)(d): handoff wait, one waiting notice, auto-resume."""

    def _handoff(self, minutes=DEFAULT_HANDOFF_WAIT_MINUTES):
        before = fields.Datetime.now()
        self._post('我要找真人', self.visitor)
        handoff = self._jobs(kind='handoff')
        self.assertEqual(len(handoff), 1)
        handoff._run_job(commit=False)
        self.assertEqual(handoff.state, 'done')
        self.assertEqual(self.session.ai_pause_reason, 'handoff')
        self.assertFalse(self.session.ai_pause_notice_sent)
        self.assertAlmostEqual(self.session.ai_paused_until,
                               before + timedelta(minutes=minutes),
                               delta=timedelta(seconds=5))
        return handoff

    def test_defaults_and_range(self):
        self.assertEqual(self.channel.ai_handoff_wait_minutes, DEFAULT_HANDOFF_WAIT_MINUTES)
        self.assertEqual(self.channel.ai_pause_notice, DEFAULT_PAUSE_NOTICE)
        for bad in (0, 1441, -5):
            with self.assertRaises(ValidationError):
                self.channel.ai_handoff_wait_minutes = bad
        self.channel.ai_handoff_wait_minutes = 1440

    def test_operator_alert_mentions_wait(self):
        ResPartner = type(self.env['res.partner'])
        self._post('轉真人', self.visitor)
        with patch.object(ResPartner, '_bus_send', autospec=True) as bus_send:
            self._jobs(kind='handoff')._run_job(commit=False)
        calls = [c for c in bus_send.call_args_list if c.args[1] == 'simple_notification']
        self.assertEqual(len(calls), 1)
        self.assertIn('waits 10 minutes', calls[0].args[2]['message'])
        self.assertIn(DEFAULT_HANDOFF_MESSAGE, self._bot_bodies()[0])

    @patch(POST)
    def test_one_notice_then_deferred_jobs(self, mock_post):
        self._handoff()
        until = self.session.ai_paused_until
        self._post('還在嗎？', self.visitor)
        self._post('我想問運費', self.visitor)
        notice = self._jobs(kind='notice')
        self.assertEqual(len(notice), 1, 'only the first message of the wait gets the notice')
        self.assertTrue(self.session.ai_pause_notice_sent)
        deferred = self._jobs(kind='reply')
        self.assertEqual(len(deferred), 2)
        self.assertTrue(all(deferred.mapped('deferred')))
        self.assertEqual(set(deferred.mapped('run_after')), {until})
        # Recovery cron triggered for the end of the wait.
        cron = self.env.ref('im_livechat_ai.ir_cron_recover_ai_jobs')
        triggers = self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)])
        self.assertTrue(any(abs((t.call_at - until).total_seconds()) <= 5 for t in triggers))
        # The notice: fixed text with the wait minutes, no LLM.
        notice._run_job(commit=False)
        self.assertEqual(notice.state, 'done')
        bodies = self._bot_bodies()
        self.assertEqual(len(bodies), 2)
        self.assertIn('the AI assistant will continue in 10 minutes', bodies[-1])
        mock_post.assert_not_called()
        # Not due: neither chained nor run early.
        self.assertFalse(self.Job._next_pending_job_id(self.session.id))
        deferred[-1]._run_job(commit=False)
        self.assertEqual(deferred[-1].state, 'pending')
        self.assertEqual(deferred[-1].attempts, 0)
        self.assertEqual(self.Job._cron_recover_jobs(commit=False), 0)
        mock_post.assert_not_called()

    @patch(POST)
    def test_auto_resume_answers_everything_once(self, mock_post):
        mock_post.return_value = _ok('運費 100 元，營業時間 9 點到 18 點。')
        self._handoff()
        self._post('運費多少？', self.visitor)
        self._jobs(kind='notice')._run_job(commit=False)
        self._post('營業時間呢？', self.visitor)
        deferred = self._jobs(kind='reply')
        # The wait ends with no operator reply; the cron runs the due jobs.
        self._expire_pause()
        self._make_due(deferred)
        self.assertEqual(self.Job._next_pending_job_id(self.session.id), deferred[-1].id)
        # The older deferred job was closed when the newer one was queued, so
        # the cron only hands over the newest one.
        self.assertEqual(self.Job._cron_recover_jobs(commit=False), 1)
        self.assertEqual(deferred.mapped('state'), ['skipped', 'done'])
        self.assertEqual(mock_post.call_count, 1)
        sent = sent_json(mock_post.call_args)['messages']
        users = [m['content'] for m in sent if m['role'] == 'user']
        self.assertEqual(users[-2:], ['運費多少？', '營業時間呢？'])
        self.assertIn(RESUME_SYSTEM_NOTE, sent[0]['content'])
        self.assertIn('運費 100 元', self._bot_bodies()[-1])
        # Nothing is answered twice.
        self.assertEqual(self.Job._cron_recover_jobs(commit=False), 0)
        self.assertEqual(mock_post.call_count, 1)

    @patch(POST)
    def test_operator_takes_over_during_wait(self, mock_post):
        self._handoff()
        self._post('請問有現貨嗎？', self.visitor)
        before = fields.Datetime.now()
        self._post('有現貨，我幫您處理', self.operator)
        self.assertEqual(self.session.ai_pause_reason, 'operator')
        self.assertGreaterEqual(self.session.ai_paused_until,
                                before + timedelta(minutes=DEFAULT_HANDOFF_PAUSE_MINUTES - 1))
        # The notice queued before the operator's reply is not sent.
        notice = self._jobs(kind='notice')
        notice._run_job(commit=False)
        self.assertEqual(notice.state, 'skipped')
        # Visitor messages during the operator pause: no job, no notice.
        self._post('謝謝', self.visitor)
        self.assertEqual(len(self._jobs(kind='reply')), 1)
        # Even after every pause ended, the deferred reply is closed quietly.
        deferred = self._jobs(kind='reply')
        self._expire_pause()
        self._make_due(deferred)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(deferred.state, 'skipped')
        self.assertIn('took over', deferred.error)
        mock_post.assert_not_called()
        self.assertEqual(len(self._bot_bodies()), 1)  # only the handoff notice

    @patch(POST)
    def test_operator_reply_shortens_long_wait_to_operator_pause(self, mock_post):
        """An operator reply during a 120-minute wait gives a 30-minute
        operator pause (not the rest of the wait); later operator replies
        only extend it."""
        self.channel.ai_handoff_wait_minutes = 120
        self._handoff(minutes=120)
        self._post('請問有現貨嗎？', self.visitor)
        deferred = self._jobs(kind='reply')
        self.assertEqual(len(deferred), 1)
        before = fields.Datetime.now()
        self._post('有現貨，我幫您處理', self.operator)
        self.assertEqual(self.session.ai_pause_reason, 'operator')
        self.assertAlmostEqual(self.session.ai_paused_until,
                               before + timedelta(minutes=DEFAULT_HANDOFF_PAUSE_MINUTES),
                               delta=timedelta(seconds=5))
        # A later operator reply never shortens the operator pause.
        later = fields.Datetime.now() + timedelta(hours=2)
        self.session.sudo().ai_paused_until = later
        self._post('還在幫您查', self.operator)
        self.assertEqual(self.session.ai_paused_until, later)
        # The deferred reply of the wait stays quiet once it is due.
        self._expire_pause()
        self._make_due(deferred)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(deferred.state, 'skipped')
        mock_post.assert_not_called()

    @patch(POST)
    def test_operator_pause_still_active_when_due(self, mock_post):
        self._handoff()
        self._post('有人嗎', self.visitor)
        self._post('我在', self.operator)
        deferred = self._jobs(kind='reply')
        self._make_due(deferred)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(deferred.state, 'skipped')
        mock_post.assert_not_called()

    def test_extended_wait_reschedules(self):
        self._handoff()
        self._post('還在嗎', self.visitor)
        deferred = self._jobs(kind='reply')
        later = fields.Datetime.now() + timedelta(minutes=45)
        self.session.sudo().ai_paused_until = later
        self._make_due(deferred)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(deferred.state, 'pending')
        self.assertEqual(deferred.run_after, later)
        self.assertEqual(deferred.attempts, 0)

    @patch(POST)
    def test_long_wait_is_not_given_up(self, mock_post):
        """A deferred job is as old as its due time, not its creation."""
        mock_post.return_value = _ok('您好')
        self.channel.ai_handoff_wait_minutes = 120
        self._handoff(minutes=120)
        self._post('在嗎', self.visitor)
        deferred = self._jobs(kind='reply')
        self.env.cr.execute(
            "UPDATE im_livechat_ai_job SET create_date = now() at time zone 'UTC' - interval '119 minutes' "
            "WHERE id = %s", (deferred.id,))
        self.env.invalidate_all()
        self._expire_pause()
        self._make_due(deferred)
        self.Job._cron_recover_jobs(commit=False)
        self.assertEqual(deferred.state, 'done')

    @patch(POST)
    def test_visitor_writes_after_wait_ended(self, mock_post):
        """The wait ended before the cron ran: the new message's normal job
        answers everything, the deferred one is superseded."""
        mock_post.return_value = _ok('好的')
        self._handoff()
        self._post('第一個問題', self.visitor)
        deferred = self._jobs(kind='reply')
        self._expire_pause()
        self._post('第二個問題', self.visitor)
        normal = self._jobs(kind='reply') - deferred
        self.assertFalse(normal.deferred)
        self._make_due(deferred)
        self.Job._cron_recover_jobs(commit=False)
        normal._run_job(commit=False)
        self.assertEqual(deferred.state, 'skipped')
        self.assertEqual(normal.state, 'done')
        self.assertEqual(mock_post.call_count, 1)

    def test_new_wait_resets_notice(self):
        self._handoff()
        self._post('在嗎', self.visitor)
        self.assertTrue(self.session.ai_pause_notice_sent)
        self._expire_pause()
        self._post('我還是要找真人', self.visitor)
        self.assertEqual(self.session.ai_pause_reason, 'handoff')
        self.assertFalse(self.session.ai_pause_notice_sent)
        self._post('哈囉', self.visitor)
        self.assertEqual(len(self._jobs(kind='notice')), 2)

    def test_notice_rendering(self):
        channel = self.channel
        channel.ai_handoff_wait_minutes = 7
        self.assertEqual(channel._ai_pause_notice_text(), DEFAULT_PAUSE_NOTICE.replace('%(minutes)s', '7'))
        for text, expected in (
            ('請稍候，客服馬上來。', '請稍候，客服馬上來。'),
            ('100% 會有人回覆，約 %(minutes)d 分鐘', '100% 會有人回覆，約 7 分鐘'),
            ('%(foo)s %s %(minutes)s %', '%(foo)s %s 7 %'),
            ('<b>%(minutes)s</b>', '<b>7</b>'),
        ):
            channel.ai_pause_notice = text
            self.assertEqual(channel._ai_pause_notice_text(), expected)
        channel.ai_pause_notice = False
        self.assertIn('continue in 7 minutes', channel._ai_pause_notice_text())

    def test_notice_html_is_escaped(self):
        self.channel.ai_pause_notice = '<script>x</script> %(minutes)s'
        self._handoff()
        self._post('在嗎', self.visitor)
        self._jobs(kind='notice')._run_job(commit=False)
        body = self._bot_bodies()[-1]
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)


class TestRangeValidation(TransactionCase):
    """Ranges are checked whether the AI is on or off; enabling AI re-validates
    the channel's own values; the runtime clamps values written around the
    constraints. History and retries are settings of the assistant and the
    connection (18.0.2.0.0)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = make_ai_channel(cls.env, {**TEST_BACKEND, 'name': 'Ranges', 'ai_enabled': False})

    def test_checked_with_ai_off(self):
        for field, bad in (('ai_max_history', 0), ('ai_max_history', 201),
                           ('ai_max_retries', 3), ('ai_max_retries', -1),
                           ('ai_handoff_pause_minutes', 0), ('ai_handoff_wait_minutes', 1441),
                           ('ai_request_timeout', 5), ('ai_request_timeout', 301)):
            with self.subTest(field=field, value=bad), self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    set_ai(self.channel, {field: bad})
                    self.env.flush_all()
        with self.assertRaises(ValidationError):
            make_ai_channel(self.env, {'name': 'Bad', 'ai_max_history': 99999})

    def test_enabling_revalidates_legacy_values(self):
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_handoff_wait_minutes = 0 "
                            "WHERE id = %s", (self.channel.id,))
        self.env.invalidate_all()
        # Unrelated writes and turning AI off still work.
        self.channel.write({'name': 'Ranges renamed', 'ai_enabled': False})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.channel.write({'ai_enabled': True})
        self.env.invalidate_all()
        self.channel.write({'ai_enabled': True, 'ai_handoff_wait_minutes': 20})
        self.assertTrue(self.channel.ai_enabled)

    def test_runtime_clamp(self):
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_ai_assistant SET max_history = 5000 WHERE id = %s",
                            (self.channel.sudo().ai_assistant_id.id,))
        self.env.invalidate_all()
        self.assertEqual(self.channel._ai_max_history_value(), 200)


class TestFreeTextModel(TransactionCase):
    """The model is free text on the AI connection (system administrators),
    required for a LiteLLM connection to be ready (18.0.1.x refused to enable
    AI without it; 18.0.2.0.0 keeps the rule through readiness and the in-use
    guard). Live Chat managers cannot change it (Q12 A in place)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(cls.env, login='r2_model_manager',
                                    groups='base.group_user,im_livechat.im_livechat_group_manager')
        cls.channel = make_ai_channel(cls.env, {
            **TEST_BACKEND,
            'name': 'Models', 'ai_enabled': True, 'ai_model': 'pool/a',
        })
        cls.connection = cls.channel.sudo().ai_assistant_id.connection_id

    def test_any_model_name_allowed(self):
        self.connection.write({'model': 'MiniMax-M1'})
        self.assertEqual(self.connection.model, 'MiniMax-M1')
        self.assertEqual(self.connection._fields['model'].type, 'char')
        with self.assertRaises(AccessError):
            self.connection.with_user(self.manager).write({'model': 'other'})

    def test_model_required_for_litellm(self):
        # in use by an AI-enabled channel: removing the model is refused
        with self.assertRaises(UserError), self.env.cr.savepoint():
            self.connection.write({'model': False})
        off = make_ai_channel(self.env, {**TEST_BACKEND, 'name': 'No Model', 'ai_model': False})
        self.assertEqual(off.sudo().ai_assistant_id.connection_id.readiness, 'blocked')
        with self.assertRaises(UserError), self.env.cr.savepoint():
            off.with_user(self.manager).write({'ai_enabled': True})
        set_ai(off, {'ai_model': 'pool/b'})
        off.with_user(self.manager).write({'ai_enabled': True})
        self.assertTrue(off.ai_enabled)

    def test_model_not_needed_for_n8n(self):
        channel = make_ai_channel(self.env, {
            'name': 'n8n', 'ai_backend_type': 'n8n', 'ai_endpoint_url': 'https://n8n.test/webhook/x',
            'ai_api_token': 'dummy-secret', 'ai_enabled': True,
        })
        self.assertTrue(channel.ai_enabled)
        self.assertTrue(channel._ai_connection_is_ready())


class TestNoSettingsPage(TransactionCase):
    """Owner decision 1: nothing of im_livechat_ai in the Settings app."""

    def test_no_settings_fields_views_or_menu(self):
        Settings = self.env['res.config.settings']
        self.assertFalse([name for name in Settings._fields if name.startswith('livechat_ai_')])
        self.assertFalse(self.env.ref('im_livechat_ai.res_config_settings_view_form_livechat_ai',
                                      raise_if_not_found=False))
        self.assertFalse(self.env.ref('im_livechat_ai.res_config_settings_action_livechat_ai',
                                      raise_if_not_found=False))
        self.assertFalse(self.env.ref('im_livechat_ai.livechat_ai_settings_menu', raise_if_not_found=False))
        self.assertFalse(self.env['ir.ui.view'].search_count([
            ('model', '=', 'res.config.settings'), ('arch_db', 'ilike', 'im_livechat_ai'),
        ]))
        settings_arch = Settings.get_views([(False, 'form')])['views']['form']['arch']
        self.assertNotIn('livechat_ai', settings_arch)
        self.assertNotIn('Live Chat AI', settings_arch)

    def test_no_system_parameters_used(self):
        self.assertFalse(self.env['ir.config_parameter'].sudo().search_count(
            [('key', '=like', 'im_livechat_ai.%')]))


class TestJobMonitoring(TransactionCase):
    """Q13 ④ (18.0.2.0.0): the recovery cron's last run and the stuck jobs are
    shown on the AI assistant form, to system administrators only; Live Chat
    managers see the channel's AI status."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cron = cls.env.ref('im_livechat_ai.ir_cron_recover_ai_jobs')
        channel = make_ai_channel(cls.env, {**TEST_BACKEND, 'name': 'Monitor', 'ai_enabled': True})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Monitor Session', 'channel_type': 'livechat',
            'livechat_operator_id': cls.env.ref('base.partner_admin').id,
            'livechat_channel_id': channel.id,
        })
        cls.channel = channel
        cls.assistant = channel.sudo().ai_assistant_id
        cls.admin = new_test_user(cls.env, login='r2_monitor_admin', groups='base.group_user,base.group_system')
        cls.manager = new_test_user(cls.env, login='r2_monitor_manager',
                                    groups='base.group_user,im_livechat.im_livechat_group_manager')

    def _monitor(self):
        assistant = self.assistant.with_user(self.admin)
        assistant.invalidate_recordset()
        return assistant

    def test_ok_when_recent_and_no_stale_jobs(self):
        self.cron.lastcall = fields.Datetime.now() - timedelta(minutes=1)
        self.env['im_livechat.ai.job'].search([]).unlink()
        assistant = self._monitor()
        self.assertFalse(assistant.job_runner_warning)
        self.assertIn('Last run of the scheduled action', assistant.job_runner_info)

    def test_warning_when_cron_late_or_jobs_stuck(self):
        self.cron.lastcall = fields.Datetime.now() - timedelta(minutes=12)
        Job = self.env['im_livechat.ai.job']
        Job.search([]).unlink()
        old = fields.Datetime.now() - timedelta(minutes=8)
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'run_after': old})
        # A run may legitimately take MAX_RUN_SECONDS: a job running for 8
        # minutes is not stuck yet, one running for 20 is.
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'state': 'running', 'started_at': old})
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'state': 'running', 'started_at': fields.Datetime.now() - timedelta(minutes=20)})
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'run_after': fields.Datetime.now() - timedelta(minutes=1)})
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'deferred': True, 'run_after': fields.Datetime.now() + timedelta(minutes=9)})
        assistant = self._monitor()
        self.assertTrue(assistant.job_runner_warning)
        self.assertIn('has not run for more than 5 minutes', assistant.job_runner_info)
        self.assertIn('2 reply job(s)', assistant.job_runner_info)

    def test_warning_when_never_run_or_inactive(self):
        self.cron.lastcall = False
        self.assertIn('has not run for more than', self._monitor().job_runner_info)
        self.cron.active = False
        self.assertIn('is disabled', self._monitor().job_runner_info)

    def test_monitoring_hidden_from_managers_and_operators(self):
        operator = new_test_user(self.env, login='r2_monitor_operator',
                                 groups='base.group_user,im_livechat.im_livechat_group_user')
        for user in (self.manager, operator):
            with self.assertRaises(AccessError):
                self.assistant.with_user(user).read(['job_runner_info'])
        # the manager sees the channel's AI status instead
        self.assertEqual(self.channel.with_user(self.manager).ai_state, 'ok')


class TestAiDisclosure(TransactionCase):
    """P1-7: bot avatar and the AI welcome message; P1-8: identity block."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.channel = make_ai_channel(cls.env, {
            **TEST_BACKEND,
            'name': 'Disclosure', 'ai_enabled': True, 'ai_bot_name': '小沃', 'ai_system_prompt': '只回答商品問題。',
        })

    def test_bot_partners_have_avatar(self):
        avatar = self.env['im_livechat.channel']._ai_bot_avatar()
        self.assertTrue(base64.b64decode(avatar).startswith(b'\x89PNG'))
        self.assertTrue(self.channel._get_bot_partner().image_1920)
        self.assertTrue(self.env.ref('im_livechat_ai.partner_ai_bot').image_1920)
        # Clearing the assistant's image gives the bot partner the robot avatar again.
        bot = self.channel._get_bot_partner()
        assistant = self.channel.sudo().ai_assistant_id
        assistant.image_1920 = False
        self.assertTrue(bot.image_1920)
        set_ai(self.channel, {'ai_bot_name': '小沃二號'})
        self.assertEqual(bot.name, '小沃二號')
        self.assertFalse(bot.active)

    def test_welcome_message(self):
        infos = self.channel._get_channel_infos()
        self.assertEqual(infos['default_message'], DEFAULT_WELCOME_MESSAGE)
        self.channel.ai_welcome_message = '嗨，我是 AI 小沃。'
        self.assertEqual(self.channel._get_channel_infos()['default_message'], '嗨，我是 AI 小沃。')
        self.channel.ai_enabled = False
        self.assertEqual(self.channel._get_channel_infos()['default_message'], self.channel.default_message)

    def test_welcome_message_for_public_visitor(self):
        public = self.env.ref('base.public_user')
        info = self.channel.with_user(public).sudo().get_livechat_info()
        if info.get('options'):
            self.assertEqual(info['options']['default_message'], DEFAULT_WELCOME_MESSAGE)

    def test_no_identity_block_or_product_rules(self):
        """18.0.1.2.0: the backend decides the persona; Odoo only sends the
        channel's own prompt."""
        session = self.env['discuss.channel'].create({
            'name': 'Identity Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.env.ref('base.partner_admin').id,
            'livechat_channel_id': self.channel.id,
        })
        self.env['mail.message'].create({
            'body': '你是誰？', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': False,
        })
        messages = session._build_llm_messages(self.channel)
        self.assertEqual(messages[0]['role'], 'system')
        self.assertEqual(split_time_line(messages[0]['content'])[0], '只回答商品問題。')
        dumped = json.dumps(messages, ensure_ascii=False)
        for text in ('不要自稱', '商品資料規則', '小沃', self.env.company.name):
            self.assertNotIn(text, dumped)
        self.assertFalse(hasattr(self.channel, '_ai_identity_block'))
