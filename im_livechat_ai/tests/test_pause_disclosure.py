# -*- coding: utf-8 -*-
"""Owner-approved round 2: explicit handoff keywords and the two pause kinds
with waiting notice and auto-resume (P0-1), range and model validation
(P1-1, P1-2), settings privacy (P1-3), job monitoring (P1-6), AI disclosure
(P1-7), the identity block (P1-8) and idempotent migration scripts (P2-4).

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
    DEFAULT_HANDOFF_KEYWORDS,
    DEFAULT_HANDOFF_MESSAGE,
    DEFAULT_HANDOFF_PAUSE_MINUTES,
    DEFAULT_HANDOFF_WAIT_MINUTES,
    DEFAULT_MODEL,
    DEFAULT_PAUSE_NOTICE,
    DEFAULT_WELCOME_MESSAGE,
    ICP_BASE_URL,
    ICP_LOG_PAYLOAD,
    ICP_LOG_RETENTION_DAYS,
    ICP_MODEL_CACHE,
    LEGACY_HANDOFF_KEYWORDS,
)

from .common import setup_litellm

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
        setup_litellm(cls.env)
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'R2 Channel',
            'ai_enabled': True,
            'ai_model': DEFAULT_MODEL,
            'ai_system_prompt': '你是客服助理。',
            'ai_product_lookup': False,
        })
        cls.bot = cls.channel.ai_bot_partner_id
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
        self.assertEqual(self.channel.ai_handoff_keywords, DEFAULT_HANDOFF_KEYWORDS)
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
        self.assertIn('等候 10 分鐘', calls[0].args[2]['message'])
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
        self.assertIn('AI 助理會在 10 分鐘後繼續為您服務', bodies[-1])
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
        self.assertEqual(self.Job._cron_recover_jobs(commit=False), 2)
        self.assertEqual(deferred.mapped('state'), ['skipped', 'done'])
        self.assertEqual(mock_post.call_count, 1)
        sent = mock_post.call_args.kwargs['json']['messages']
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
        self.assertIn('真人客服已接手', deferred.error)
        mock_post.assert_not_called()
        self.assertEqual(len(self._bot_bodies()), 1)  # only the handoff notice

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
        self.assertIn('7 分鐘', channel._ai_pause_notice_text())

    def test_notice_html_is_escaped(self):
        self.channel.ai_pause_notice = '<script>x</script> %(minutes)s'
        self._handoff()
        self._post('在嗎', self.visitor)
        self._jobs(kind='notice')._run_job(commit=False)
        body = self._bot_bodies()[-1]
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)


class TestRangeValidation(TransactionCase):
    """P1-1: ranges checked with AI off; enabling AI re-validates; runtime clamp."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.channel = cls.env['im_livechat.channel'].create({'name': 'Ranges', 'ai_enabled': False})

    def test_checked_with_ai_off(self):
        for field, bad in (('ai_max_tokens', 0), ('ai_max_tokens', 8193), ('ai_max_history', 0),
                           ('ai_max_history', 201), ('ai_temperature', 2.5), ('ai_temperature', -0.1),
                           ('ai_max_retries', 3), ('ai_max_retries', -1),
                           ('ai_handoff_pause_minutes', 0), ('ai_handoff_wait_minutes', 1441)):
            with self.subTest(field=field, value=bad), self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.channel.write({field: bad})
        with self.assertRaises(ValidationError):
            self.env['im_livechat.channel'].create({'name': 'Bad', 'ai_max_tokens': 99999})

    def test_enabling_revalidates_legacy_values(self):
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_max_history = 0, ai_temperature = 7 "
                            "WHERE id = %s", (self.channel.id,))
        self.env.invalidate_all()
        # Unrelated writes and turning AI off still work.
        self.channel.write({'name': 'Ranges renamed', 'ai_enabled': False})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.channel.write({'ai_enabled': True})
        self.env.invalidate_all()
        self.channel.write({'ai_enabled': True, 'ai_max_history': 20, 'ai_temperature': 0.5})
        self.assertTrue(self.channel.ai_enabled)

    @patch(POST)
    def test_runtime_clamp(self, mock_post):
        mock_post.return_value = _ok('OK')
        self.channel.write({'ai_enabled': True})
        cases = ((99999, 9.5, 8192, 2.0), (0, -3, 1024, 0.0), (300, 0.4, 300, 0.4))
        for tokens, temperature, exp_tokens, exp_temperature in cases:
            self.env.cr.execute(
                "UPDATE im_livechat_channel SET ai_max_tokens = %s, ai_temperature = %s WHERE id = %s",
                (tokens, temperature, self.channel.id))
            self.env.invalidate_all()
            self.channel._call_llm_api([{'role': 'user', 'content': 'hi'}])
            payload = mock_post.call_args.kwargs['json']
            self.assertEqual(payload['max_tokens'], exp_tokens)
            self.assertAlmostEqual(payload['temperature'], exp_temperature)
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_max_history = 5000 WHERE id = %s",
                            (self.channel.id,))
        self.env.invalidate_all()
        self.assertEqual(self.channel._ai_max_history_value(), 200)


class TestModelValidation(TransactionCase):
    """P1-2: the model must come from the cached LiteLLM list (or be the default)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env, models=['pool/a', 'pool/b'])
        cls.manager = new_test_user(cls.env, login='r2_model_manager',
                                    groups='base.group_user,im_livechat.im_livechat_group_manager')
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Models', 'ai_enabled': True, 'ai_model': 'pool/a',
        })

    def test_cached_and_default_allowed(self):
        self.channel.with_user(self.manager).write({'ai_model': 'pool/b'})
        self.channel.with_user(self.manager).write({'ai_model': DEFAULT_MODEL})
        self.assertEqual(self.channel.ai_model, DEFAULT_MODEL)

    def test_unknown_model_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            self.channel.with_user(self.manager).write({'ai_model': 'pool/zzz'})
        self.assertIn('不在可用模型清單中', str(caught.exception))
        with self.assertRaises(ValidationError):
            self.env['im_livechat.channel'].create({'name': 'X', 'ai_model': 'pool/zzz'})
        # Also with AI off: the value is set/changed.
        off = self.env['im_livechat.channel'].create({'name': 'Off'})
        with self.assertRaises(ValidationError):
            off.write({'ai_model': 'nope/model'})

    def test_legacy_value_does_not_block_other_fields(self):
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_model = 'MiniMax-M1', ai_enabled = FALSE "
                            "WHERE id = %s", (self.channel.id,))
        self.env.invalidate_all()
        self.channel.with_user(self.manager).write({'ai_system_prompt': '新提示', 'ai_max_tokens': 500})
        self.channel.with_user(self.manager).write({'ai_model': 'MiniMax-M1'})  # unchanged value
        # Enabling AI with the legacy model is refused, choosing a listed one works.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.channel.with_user(self.manager).write({'ai_enabled': True})
        self.env.invalidate_all()
        self.channel.with_user(self.manager).write({'ai_enabled': True, 'ai_model': 'pool/b'})
        self.assertTrue(self.channel.ai_enabled)

    def test_enabled_channel_with_legacy_value_can_be_edited(self):
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_model = 'MiniMax-M1' WHERE id = %s",
                            (self.channel.id,))
        self.env.invalidate_all()
        self.channel.with_user(self.manager).write({'ai_handoff_wait_minutes': 15, 'ai_enabled': True})
        self.assertEqual(self.channel.ai_handoff_wait_minutes, 15)


class TestSettingsPrivacy(TransactionCase):
    """P1-3: non-system users never get the LiteLLM settings through RPC."""

    FIELDS = ['livechat_ai_base_url', 'livechat_ai_log_payload', 'livechat_ai_log_retention_days',
              'livechat_ai_model_list', 'livechat_ai_api_key', 'livechat_ai_api_key_set',
              'livechat_ai_cron_lastcall', 'livechat_ai_stale_job_count', 'livechat_ai_job_warning']

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env, base_url='https://internal-litellm.example/v1', log_payload=True)
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param(ICP_LOG_RETENTION_DAYS, 21)
        ICP.set_param(ICP_MODEL_CACHE, json.dumps(['secret/model']))
        cls.portal = new_test_user(cls.env, login='r2_portal', groups='base.group_portal')
        cls.manager = new_test_user(cls.env, login='r2_lc_manager',
                                    groups='base.group_user,im_livechat.im_livechat_group_manager')
        cls.admin = new_test_user(cls.env, login='r2_sysadmin', groups='base.group_user,base.group_system')

    def _assert_no_ai_values(self, values):
        leaked = [name for name in values if name.startswith('livechat_ai_')]
        self.assertFalse(leaked)
        dumped = json.dumps(values, default=str)
        for secret in ('internal-litellm', 'secret/model'):
            self.assertNotIn(secret, dumped)

    def test_portal_and_manager_get_nothing(self):
        for user in (self.portal, self.manager):
            Settings = self.env['res.config.settings'].with_user(user)
            self._assert_no_ai_values(Settings.default_get(self.FIELDS))
            self._assert_no_ai_values(Settings.browse().get_values())
            try:
                self.assertFalse(set(Settings.fields_get(self.FIELDS)))
            except AccessError:
                pass
            with self.assertRaises(AccessError):
                Settings.onchange({}, [], {name: {} for name in self.FIELDS})
            with self.assertRaises(AccessError):
                Settings.create({})
            with self.assertRaises(AccessError):
                Settings.browse().set_values()
        # Nothing was changed by the attempts.
        ICP = self.env['ir.config_parameter'].sudo()
        self.assertEqual(ICP.get_param(ICP_BASE_URL), 'https://internal-litellm.example/v1')
        self.assertEqual(ICP.get_param(ICP_LOG_PAYLOAD), 'True')

    def test_system_admin_sees_and_edits(self):
        Settings = self.env['res.config.settings'].with_user(self.admin)
        values = Settings.default_get(self.FIELDS)
        self.assertEqual(values['livechat_ai_base_url'], 'https://internal-litellm.example/v1')
        self.assertTrue(values['livechat_ai_log_payload'])
        self.assertEqual(values['livechat_ai_log_retention_days'], 21)
        settings = Settings.create({'livechat_ai_log_retention_days': 30})
        self.assertEqual(settings.livechat_ai_model_list, 'secret/model')
        settings.set_values()
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(ICP_LOG_RETENTION_DAYS), '30')


class TestJobMonitoring(TransactionCase):
    """P1-6: last run of the recovery cron and stuck jobs in the settings."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.cron = cls.env.ref('im_livechat_ai.ir_cron_recover_ai_jobs')
        channel = cls.env['im_livechat.channel'].create({'name': 'Monitor', 'ai_enabled': True})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Monitor Session', 'channel_type': 'livechat',
            'livechat_operator_id': cls.env.ref('base.partner_admin').id,
            'livechat_channel_id': channel.id,
        })
        cls.channel = channel

    def _settings(self):
        return self.env['res.config.settings'].create({})

    def test_ok_when_recent_and_no_stale_jobs(self):
        self.cron.lastcall = fields.Datetime.now() - timedelta(minutes=1)
        self.env['im_livechat.ai.job'].search([]).unlink()
        settings = self._settings()
        self.assertEqual(settings.livechat_ai_cron_lastcall, self.cron.lastcall)
        self.assertEqual(settings.livechat_ai_stale_job_count, 0)
        self.assertFalse(settings.livechat_ai_job_warning)

    def test_warning_when_cron_late_or_jobs_stuck(self):
        self.cron.lastcall = fields.Datetime.now() - timedelta(minutes=12)
        Job = self.env['im_livechat.ai.job']
        Job.search([]).unlink()
        old = fields.Datetime.now() - timedelta(minutes=8)
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'run_after': old})
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'state': 'running', 'started_at': old})
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'run_after': fields.Datetime.now() - timedelta(minutes=1)})
        Job.create({'discuss_channel_id': self.session.id, 'livechat_channel_id': self.channel.id,
                    'deferred': True, 'run_after': fields.Datetime.now() + timedelta(minutes=9)})
        settings = self._settings()
        self.assertEqual(settings.livechat_ai_stale_job_count, 2)
        self.assertIn('超過 5 分鐘沒有執行', settings.livechat_ai_job_warning)
        self.assertIn('2 個 AI 回覆工作', settings.livechat_ai_job_warning)

    def test_warning_when_never_run_or_inactive(self):
        self.cron.lastcall = False
        self.assertIn('沒有執行', self._settings().livechat_ai_job_warning)
        self.cron.active = False
        self.assertIn('已停用', self._settings().livechat_ai_job_warning)

    def test_settings_form_onchange_shows_values(self):
        """The settings form loads through onchange() (its view has company_id,
        from base_setup): the computed values must be there."""
        self.cron.lastcall = fields.Datetime.now() - timedelta(minutes=12)
        spec = {name: {} for name in ('company_id', 'livechat_ai_cron_lastcall', 'livechat_ai_stale_job_count',
                                      'livechat_ai_job_warning', 'livechat_ai_api_key_set')}
        value = self.env['res.config.settings'].onchange({}, [], spec)['value']
        self.assertTrue(value['livechat_ai_cron_lastcall'])
        self.assertIn('超過 5 分鐘沒有執行', value['livechat_ai_job_warning'])
        self.assertTrue(value['livechat_ai_api_key_set'])

    def test_monitoring_hidden_from_non_admins(self):
        manager = new_test_user(self.env, login='r2_monitor_manager',
                                groups='base.group_user,im_livechat.im_livechat_group_manager')
        values = self.env['res.config.settings'].with_user(manager).default_get(
            ['livechat_ai_cron_lastcall', 'livechat_ai_job_warning'])
        self.assertFalse([k for k in values if k.startswith('livechat_ai_')])


class TestAiDisclosure(TransactionCase):
    """P1-7: bot avatar and the AI welcome message; P1-8: identity block."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Disclosure', 'ai_enabled': True, 'ai_bot_name': '小沃', 'ai_system_prompt': '只回答商品問題。',
        })

    def test_bot_partners_have_avatar(self):
        avatar = self.env['im_livechat.channel']._ai_bot_avatar()
        self.assertTrue(base64.b64decode(avatar).startswith(b'\x89PNG'))
        self.assertTrue(self.channel.ai_bot_partner_id.image_1920)
        self.assertTrue(self.env.ref('im_livechat_ai.partner_ai_bot').image_1920)
        # An existing bot without image gets it when AI is (re)enabled.
        bot = self.channel.ai_bot_partner_id
        bot.image_1920 = False
        self.channel.write({'ai_bot_name': '小沃二號'})
        self.assertTrue(bot.image_1920)

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

    def test_identity_block_first(self):
        session = self.env['discuss.channel'].create({
            'name': 'Identity Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.env.ref('base.partner_admin').id,
            'livechat_channel_id': self.channel.id,
        })
        system = session._build_llm_messages(self.channel)[0]['content']
        company = self.channel._ai_company_name()
        self.assertTrue(company)
        self.assertTrue(system.startswith(f'你是 {company} 的 AI 客服助理「小沃」。被問到身分時就這樣回答；'))
        self.assertIn('不要自稱 ChatGPT、GPT、Codex、OpenAI 或其他產品／模型名稱。', system)
        self.assertLess(system.index('不要自稱'), system.index('只回答商品問題。'))
        self.assertLess(system.index('只回答商品問題。'), system.index('【商品資料規則】'))

    def test_identity_uses_website_name(self):
        if 'website' not in self.env or 'channel_id' not in self.env['website']._fields:
            self.skipTest('website_livechat not installed')
        website = self.env['website'].create({'name': '沃科技商店', 'channel_id': self.channel.id})
        self.assertEqual(self.channel._ai_company_name(), website.name)


class TestMigrationIdempotency(TransactionCase):
    """P2-4: the legacy-only steps need the flag from pre-migrate; a re-run on
    a migrated database changes nothing."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_litellm(cls.env)
        cls.pre = _load_migration('pre-migrate')
        cls.post = _load_migration('post-migrate')
        Channel = cls.env['im_livechat.channel']
        cls.on = Channel.create({'name': 'Migrated On', 'ai_enabled': True, 'ai_max_retries': 1})
        cls.custom = Channel.create({'name': 'Custom Keywords', 'ai_handoff_keywords': '專人,manager'})
        cls.log = cls.env['llm.api.log'].create({'status': 'success', 'request_payload': '[{"x": 1}]'})

    def _snapshot(self):
        cr = self.env.cr
        self.env.flush_all()
        cr.execute("""SELECT id, ai_enabled, ai_max_retries, ai_max_tokens, ai_handoff_keywords,
                             ai_error_message, ai_bot_name, ai_bot_partner_id
                        FROM im_livechat_channel ORDER BY id""")
        channels = cr.fetchall()
        cr.execute("SELECT id, request_payload, error_message FROM llm_api_log ORDER BY id")
        logs = cr.fetchall()
        cr.execute("""SELECT p.id, p.name, p.active, md5(coalesce(a.db_datas::text, a.store_fname, ''))
                        FROM res_partner p
                        LEFT JOIN ir_attachment a ON a.res_model = 'res.partner' AND a.res_id = p.id
                                                AND a.res_field = 'image_1920'
                       WHERE p.id IN (SELECT ai_bot_partner_id FROM im_livechat_channel)
                    ORDER BY p.id""")
        partners = cr.fetchall()
        cr.execute("SELECT count(*) FROM ir_config_parameter WHERE key = %s", (self.pre.LEGACY_FLAG_KEY,))
        return channels, logs, partners, cr.fetchone()[0]

    def test_rerun_on_migrated_db_changes_nothing(self):
        before = self._snapshot()
        for _i in range(2):
            self.pre.migrate(self.env.cr, '18.0.1.1.0')
            self.post.migrate(self.env.cr, '18.0.1.1.0')
            self.env.invalidate_all()
            self.assertEqual(self._snapshot(), before)
        self.assertTrue(self.on.ai_enabled)
        self.assertEqual(self.on.ai_max_retries, 1)
        self.assertEqual(self.log.request_payload, '[{"x": 1}]')

    def test_legacy_schema_sets_flag_and_converts(self):
        self.env.cr.execute(SQL("ALTER TABLE im_livechat_channel ADD COLUMN %s integer",
                                SQL.identifier('ai_retry_delay')))
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", (self.pre.LEGACY_FLAG_KEY,))
        self.assertEqual(self.env.cr.fetchone()[0], '1')
        with self.assertLogs(self.post.__name__, level='WARNING'):
            self.post.migrate(self.env.cr, '18.0.1.0.1')
        self.env.invalidate_all()
        self.assertFalse(sql.column_exists(self.env.cr, 'im_livechat_channel', 'ai_retry_delay'))
        self.assertFalse(self.on.ai_enabled)
        self.assertEqual(self.on.ai_max_retries, 0)  # 1 total call -> 0 retries
        self.assertFalse(self.log.request_payload)
        self.env.cr.execute("SELECT count(*) FROM ir_config_parameter WHERE key = %s", (self.pre.LEGACY_FLAG_KEY,))
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        # A second run (no legacy columns any more) changes nothing.
        self.on.write({'ai_enabled': True})
        before = self._snapshot()
        self.pre.migrate(self.env.cr, '18.0.1.0.1')
        self.post.migrate(self.env.cr, '18.0.1.0.1')
        self.env.invalidate_all()
        self.assertEqual(self._snapshot(), before)

    def test_handoff_keywords_migration(self):
        Channel = self.env['im_livechat.channel']
        old = Channel.create({'name': 'Old Keywords'})
        empty = Channel.create({'name': 'No Keywords'})
        spaced = Channel.create({'name': 'Spaced Old Keywords'})
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_handoff_keywords = %s WHERE id = %s",
                            (LEGACY_HANDOFF_KEYWORDS, old.id))
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_handoff_keywords = NULL WHERE id = %s",
                            (empty.id,))
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_handoff_keywords = %s WHERE id = %s",
                            (LEGACY_HANDOFF_KEYWORDS.replace(',', ', '), spaced.id))
        self.assertEqual(self.post.LEGACY_HANDOFF_KEYWORDS, LEGACY_HANDOFF_KEYWORDS)
        self.assertEqual(self.post.NEW_HANDOFF_KEYWORDS, DEFAULT_HANDOFF_KEYWORDS)
        changed = self.post.migrate_handoff_keywords(self.env.cr)
        self.env.invalidate_all()
        self.assertEqual(set(changed), {old.id, empty.id, spaced.id})
        for channel in (old, empty, spaced):
            self.assertEqual(channel.ai_handoff_keywords, DEFAULT_HANDOFF_KEYWORDS)
        self.assertEqual(self.custom.ai_handoff_keywords, '專人,manager')
        self.assertFalse(self.post.migrate_handoff_keywords(self.env.cr))

    def test_bot_avatars_migration(self):
        bot = self.on.ai_bot_partner_id
        bot.image_1920 = False
        default_bot = self.env.ref('im_livechat_ai.partner_ai_bot')
        default_bot.image_1920 = False
        real = new_test_user(self.env, login='r2_real_person').partner_id
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_bot_partner_id = %s WHERE id = %s",
                            (real.id, self.custom.id))
        self.env.invalidate_all()
        self.assertGreaterEqual(self.post.set_bot_avatars(self.env.cr), 2)
        self.env.invalidate_all()
        self.assertTrue(bot.image_1920)
        self.assertTrue(default_bot.image_1920)
        self.assertFalse(real.image_1920 and real.image_1920 == bot.image_1920)
        self.assertEqual(self.post.set_bot_avatars(self.env.cr), 0)
