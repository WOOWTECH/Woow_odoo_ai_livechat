# -*- coding: utf-8 -*-
"""Contract v1.3: the file fallback (CONTRACT.md 3.7, check 15; 18.0.2.1.0).

The staging bench (2026-11-28): one file the LiteLLM route could not read (HTTP 500 for a PDF, 400 and 422 for a
.txt) made every later turn fail, because the files of the last visitor messages go with every request. Now a
LiteLLM request with file parts (inline images, documents in 'file' mode) that is refused with HTTP 400, 413,
415, 422 or 500 is sent again at once: without the file parts of the earlier visitor messages, then without any
file part. A file left out is its ``[附件]`` line and the note
``[附件說明] <name>：模型無法讀取這個檔案，只附下載連結。``; no wait, not counted in the retries, within the
answer's deadline (none starts with less than one second left); each request writes its own call log; the answer
is the first success. When a fallback request succeeds, the conversation remembers the files it left out
(im_livechat.ai.job.file_fallback_ids, committed before the reply is posted, read by every later request and by a
re-run of the job); when no request succeeds, nothing is remembered. Other statuses, requests without files and
n8n are handled as before. All HTTP is mocked.
"""

import base64
import json
from unittest.mock import MagicMock, patch

import requests
from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from odoo.addons.im_livechat_ai.models import discuss_channel as dc
from odoo.addons.im_livechat_ai.models.ai_connection import NO_IN_USE_CHECK
from odoo.addons.im_livechat_ai.models.livechat_ai_job import BLOCKED_AT_RUN_NOTE

from odoo.addons.im_livechat_ai.tools.backend import time_line

from .common import CLOCK, SATURDAY_MORNING, call_backend, ok_response, set_ai, split_time_line
from .test_attachments import PDF, POST, TINY_PNG, AttachmentCase

SLEEP = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep'
CHANNEL_LOGGER = 'odoo.addons.im_livechat_ai.models.im_livechat_channel'
NOTE = '模型無法讀取這個檔案，只附下載連結。'
# a second 1x1 PNG with other bytes than TINY_PNG (another color), and a second PDF
OTHER_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
OTHER_PDF = PDF + b'% another document\n'
CLOCK_TIME = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.time'
# The text of an unexpected exception: never logged (contract 3.9). Raised through this name, so that no code frame
# of the test shows it as a literal.
EXCEPTION_TEXT = 'SECRET-' + '7f3a'


class _WorkerKilled(BaseException):
    """The process running the job dies (not an Exception: nothing in the module catches it)."""


def status_response(code):
    response = MagicMock()
    response.status_code = code
    response.json.return_value = {'error': {'message': 'the model cannot read this file'}}
    return response


def file_parts(payload):
    return [part for turn in payload['messages'] if isinstance(turn.get('content'), list)
            for part in turn['content'] if part.get('type') in ('image_url', 'file')]


def refusing(code=400, answer='好的'):
    """A backend that answers HTTP ``code`` to every request with a file part, and ``answer`` otherwise."""
    def post(*_args, **kwargs):
        return status_response(code) if file_parts(json.loads(kwargs['data'])) else ok_response(answer)
    return post


class FileFallbackCase(AttachmentCase):

    def turn(self, session, message, side_effect):
        """Queue the reply job of ``message`` and run it once: (job, payloads sent, sleep mock)."""
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        with patch(POST, side_effect=side_effect) as post, patch(SLEEP) as sleep, mute_logger(CHANNEL_LOGGER):
            job._run_job(commit=False)
        return job, [json.loads(call.kwargs['data']) for call in post.call_args_list], sleep

    def logs(self, job):
        logs = self.env['llm.api.log'].sudo().search([('job_id', '=', job.id)], order='id')
        return [(log.status, log.http_status) for log in logs]

    def bot_bodies(self, session):
        bot = self.channel._get_bot_partner()
        return self.env['mail.message'].search([('model', '=', 'discuss.channel'), ('res_id', '=', session.id),
                                                ('author_id', '=', bot.id)], order='id').mapped('body')

    def turn_text(self, payload, needle):
        """The text of the user turn of ``payload`` that contains ``needle``."""
        texts = [self.text_of(turn['content']) for turn in payload['messages'][1:] if turn['role'] == 'user']
        found = [text for text in texts if needle in text]
        self.assertTrue(found, needle)
        return found[-1]

    def assert_link_and_note(self, text, attachment):
        self.assert_signed_link(text, attachment)
        self.assertIn('[附件] %s（' % attachment.name, text)
        self.assertIn('[附件說明] %s：%s' % (attachment.name, NOTE), text)

    def kill(self, job, side_effect, *patches):
        """Run ``job`` once; the process dies (_WorkerKilled) at the point ``side_effect`` or ``patches`` set up.
        Not self.assertRaises: Odoo's rolls back a savepoint, while the runner commits before each request and
        before the post (here: flushes), so the run leaves what a real process death leaves."""
        try:
            with patch(POST, side_effect=side_effect), patch(SLEEP), mute_logger(CHANNEL_LOGGER):
                for started in patches:
                    started.start()
                try:
                    job._run_job(commit=False)
                finally:
                    for started in patches:
                        started.stop()
        except _WorkerKilled:
            pass
        else:
            self.fail('the run was not killed')
        self.env.invalidate_all()

    def call_with_clock(self, session, side_effect, timeout):
        """One answer (no job) with ``timeout`` seconds and a clock the backend moves (``side_effect(clock)``)."""
        set_ai(self.channel, {'ai_request_timeout': timeout})
        clock = [1000.0]
        with patch(POST, side_effect=lambda *args, **kwargs: side_effect(clock, kwargs)) as post, \
                mute_logger(CHANNEL_LOGGER), patch(CLOCK_TIME, side_effect=lambda: clock[0]):
            result = call_backend(self.channel, session=session)
        return result, [json.loads(call.kwargs['data']) for call in post.call_args_list]


class TestFileFallback(FileFallbackCase):

    def test_each_status_sends_the_turn_again_without_the_earlier_image(self):
        for code in (400, 413, 415, 422, 500):
            with self.subTest(status=code):
                session = self.session()
                _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
                message, _a = self.post(session, '可以再說一次嗎？')
                job, sent, sleep = self.turn(session, message, refusing(code))
                self.assertEqual(len(sent), 2, 'with the image, then again without it')
                self.assertEqual([part['type'] for part in file_parts(sent[0])], ['image_url'])
                self.assertFalse(file_parts(sent[1]))
                self.assert_link_and_note(self.turn_text(sent[1], '這是什麼？'), image)
                self.assertEqual(sent[1]['messages'][-1], {'role': 'user', 'content': '可以再說一次嗎？'})
                self.assertFalse(sleep.called, 'sent again at once')
                self.assertEqual(job.state, 'done', job.error)
                self.assertEqual(job.llm_calls, 2)
                self.assertEqual(self.bot_bodies(session), ['<p>好的</p>'])
                self.assertEqual(self.logs(job), [('http_error', code), ('ok', 200)], 'one call log per request')
                self.assertEqual(job.file_fallback_ids, str(image.id))

    def test_other_answers_do_not_start_it(self):
        """401, 404, 502, 504 and a 429 after the retries: the turn fails as before, the image is not left out."""
        set_ai(self.channel, {'ai_max_retries': 1})
        for label, effects in (('401', [status_response(401)]), ('404', [status_response(404)]),
                               ('502', [status_response(502)]), ('504', [status_response(504)]),
                               ('429 twice', [status_response(429), status_response(429)])):
            with self.subTest(answer=label):
                session = self.session()
                self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
                message, _a = self.post(session, '然後呢？')
                job, sent, _sleep = self.turn(session, message, effects)
                self.assertEqual(len(sent), len(effects))
                self.assertTrue(all(file_parts(payload) for payload in sent), 'never without the image')
                self.assertEqual(job.state, 'failed')
                self.assertFalse(job.file_fallback_ids)
                self.assertEqual(len(self.logs(job)), 1)

    def test_a_request_without_files_never_starts_it(self):
        session = self.session()
        message, _a = self.post(session, '你好')
        job, sent, _sleep = self.turn(session, message, [status_response(400), ok_response('不會到')])
        self.assertEqual(len(sent), 1)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.logs(job), [('http_error', 400)])
        self.assertFalse(job.file_fallback_ids)

    def test_documents_in_file_mode(self):
        session = self.session()
        _m, (pdf,) = self.post(session, '請看報價單', [('報價單.pdf', PDF, 'application/pdf')])
        message, _a = self.post(session, '有折扣嗎？')
        job, sent, _sleep = self.turn(session, message, refusing(500))
        self.assertEqual([part['type'] for part in file_parts(sent[0])], ['file'])
        self.assertIn('檔案已附在這則訊息', self.turn_text(sent[0], '請看報價單'))
        text = self.turn_text(sent[1], '請看報價單')
        self.assertNotIn('檔案已附在這則訊息', text)
        self.assert_link_and_note(text, pdf)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(job.file_fallback_ids, str(pdf.id))
        # the next turn sends the document as its link and the note, in one request
        message, _a = self.post(session, '那運費呢？')
        _second, sent, _sleep = self.turn(session, message, refusing(500))
        self.assertEqual(len(sent), 1)
        self.assertFalse(file_parts(sent[0]))
        self.assert_link_and_note(self.turn_text(sent[0], '請看報價單'), pdf)

    def test_then_without_the_current_message_file(self):
        """Step 2: the request without the earlier files is refused too while the newest visitor message still
        has one: the turn goes once more without any file part."""
        session = self.session()
        _m, (pdf,) = self.post(session, '請看報價單', [('報價單.pdf', PDF, 'application/pdf')])
        message, (image,) = self.post(session, '還有這張圖', [('photo.png', TINY_PNG, 'image/png')])
        job, sent, sleep = self.turn(session, message, refusing(422))
        self.assertEqual([sorted(part['type'] for part in file_parts(payload)) for payload in sent],
                         [['file', 'image_url'], ['image_url'], []])
        self.assert_link_and_note(self.turn_text(sent[1], '請看報價單'), pdf)
        self.assertNotIn('[附件說明] photo.png', self.turn_text(sent[1], '還有這張圖'))
        self.assert_link_and_note(self.turn_text(sent[2], '請看報價單'), pdf)
        self.assert_link_and_note(self.turn_text(sent[2], '還有這張圖'), image)
        self.assertFalse(sleep.called)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(self.logs(job), [('http_error', 422), ('http_error', 422), ('ok', 200)])
        self.assertEqual(job.file_fallback_ids, '%s,%s' % tuple(sorted((pdf.id, image.id))))

    def test_only_the_current_message_file(self):
        """No earlier file part: the one fallback step leaves the newest message's file out."""
        session = self.session()
        message, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        job, sent, _sleep = self.turn(session, message, refusing(400))
        self.assertEqual(len(sent), 2)
        self.assertFalse(file_parts(sent[1]))
        self.assert_link_and_note(self.text_of(sent[1]['messages'][-1]['content']), image)
        self.assertEqual(job.file_fallback_ids, str(image.id))

    def test_the_current_message_file_stays_when_the_first_step_answers(self):
        session = self.session()
        _m, (old,) = self.post(session, '舊圖', [('old.png', OTHER_PNG, 'image/png')])
        message, (new,) = self.post(session, '新圖', [('new.png', TINY_PNG, 'image/png')])
        old_uri = 'data:image/png;base64,' + base64.b64encode(OTHER_PNG).decode()

        def refuses_the_old_image(*_args, **kwargs):
            payload = json.loads(kwargs['data'])
            urls = [part['image_url']['url'] for part in file_parts(payload)]
            return status_response(400) if old_uri in urls else ok_response('好的')
        job, sent, _sleep = self.turn(session, message, refuses_the_old_image)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(file_parts(sent[1])), 1, 'the newest message keeps its image')
        self.assert_link_and_note(self.turn_text(sent[1], '舊圖'), old)
        self.assertEqual(job.file_fallback_ids, str(old.id))
        self.assertNotIn(str(new.id), job.file_fallback_ids.split(','))

    def test_retries_first_and_not_counted_in_them(self):
        """HTTP 429 is retried as before (3 s); the refused request is then sent again without the file at
        once, also with no retry left."""
        set_ai(self.channel, {'ai_max_retries': 1})
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '然後呢？')
        job, sent, sleep = self.turn(session, message, [status_response(429), status_response(400),
                                                        ok_response('好的')])
        self.assertEqual([bool(file_parts(payload)) for payload in sent], [True, True, False])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3])
        self.assertEqual(job.state, 'done', job.error)
        logs = self.env['llm.api.log'].sudo().search([('job_id', '=', job.id)], order='id')
        self.assertEqual([(log.status, log.http_status, log.retry_count) for log in logs],
                         [('http_error', 400, 1), ('ok', 200, 0)])
        set_ai(self.channel, {'ai_max_retries': 0})
        session = self.session()
        message, _a = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        job, sent, _sleep = self.turn(session, message, refusing(400))
        self.assertEqual(len(sent), 2, 'no retry left: the fallback still sends the turn again')
        self.assertEqual(job.state, 'done', job.error)

    def test_every_step_refused_fails_as_before(self):
        """No request succeeds (the backend may be down for every request): the visitor gets the error message,
        as before, and nothing is remembered."""
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '然後呢？')
        job, sent, _sleep = self.turn(session, message, [status_response(500), status_response(500)])
        self.assertEqual(len(sent), 2)
        self.assertEqual(job.state, 'failed')
        self.assertIn('HTTP 500', job.error)
        self.assertEqual(self.logs(job), [('http_error', 500), ('http_error', 500)])
        self.assertEqual(len(self.bot_bodies(session)), 1, 'the error message')
        self.assertFalse(job.file_fallback_ids)
        self.assertEqual(session._ai_file_fallback_ids(), set())

    def test_a_fallback_request_failing_otherwise_stores_nothing(self):
        """The request without the file fails another way (401, a read timeout): the turn fails as before and
        nothing is remembered."""
        for label, failure in (('401', status_response(401)), ('timeout', requests.ReadTimeout('slow'))):
            with self.subTest(failure=label):
                session = self.session()
                self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
                message, _a = self.post(session, '然後呢？')
                job, sent, _sleep = self.turn(session, message, [status_response(400), failure])
                self.assertEqual([bool(file_parts(payload)) for payload in sent], [True, False])
                self.assertEqual(job.state, 'failed')
                self.assertFalse(job.file_fallback_ids)
                self.assertEqual(session._ai_file_fallback_ids(), set())

    def test_the_deadline_stops_it(self):
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        self.post(session, '然後呢？')

        def refusal_after(seconds):
            def post(clock, _kwargs):
                clock[0] += seconds
                return status_response(400)
            return post
        # the refusal came after the deadline, or with less than one second left: no fallback request
        for seconds in (13, 11.5):
            with self.subTest(seconds=seconds):
                result, sent = self.call_with_clock(session, refusal_after(seconds), timeout=12)
                self.assertEqual(len(sent), 1)
                self.assertIn('HTTP 400', result['error'])
                self.assertEqual(result['left_out'], [])

        def refusal_then_answer(clock, kwargs):
            clock[0] += 10.5  # 1.5 s left: the fallback request is sent
            return status_response(400) if file_parts(json.loads(kwargs['data'])) else ok_response('好的')
        result, sent = self.call_with_clock(session, refusal_then_answer, timeout=12)
        self.assertEqual(len(sent), 2)
        self.assertFalse(result['error'])

    def test_one_second_checked_again_before_sending(self):
        """The rule of the last second is checked again right before a fallback request is sent: here the call
        log, the gate and the build of the request leave 0.7 s, so it is not sent (and leaves no call log)."""
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        self.post(session, '然後呢？')
        set_ai(self.channel, {'ai_request_timeout': 12})
        Channel = type(self.env['im_livechat.channel'])
        gate_open = Channel._ai_gate_open
        clock = [1000.0]

        def refusal(*_args, **_kwargs):
            clock[0] += 10.5  # 1.5 s left when the fallback is decided
            return status_response(400)

        def slow_gate(channel):
            clock[0] += 0.8
            return gate_open(channel)
        first_log = self.env['llm.api.log'].sudo().search([], order='id desc', limit=1).id or 0
        with patch(POST, side_effect=refusal) as post, mute_logger(CHANNEL_LOGGER), \
                patch(CLOCK_TIME, side_effect=lambda: clock[0]), patch.object(Channel, '_ai_gate_open', slow_gate):
            result = call_backend(self.channel, session=session)
        self.assertEqual(post.call_count, 1, 'no fallback request with 0.7 s left')
        self.assertIn('HTTP 400', result['error'])
        logs = self.env['llm.api.log'].sudo().search([('id', '>', first_log)])
        self.assertEqual(logs.mapped('http_status'), [400], 'only the request that was sent has a call log')

    def test_later_request_timeouts_are_capped_by_the_time_left(self):
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        self.post(session, '然後呢？')

        def refusal_then_answer(clock, kwargs):
            if file_parts(json.loads(kwargs['data'])):
                clock[0] += 7  # 5 s left of the 12
                return status_response(400)
            return ok_response('好的')
        set_ai(self.channel, {'ai_request_timeout': 12})
        clock = [1000.0]
        with patch(POST, side_effect=lambda *args, **kwargs: refusal_then_answer(clock, kwargs)) as post, \
                mute_logger(CHANNEL_LOGGER), patch(CLOCK_TIME, side_effect=lambda: clock[0]):
            result = call_backend(self.channel, session=session)
        self.assertFalse(result['error'])
        self.assertEqual([call.kwargs['timeout'] for call in post.call_args_list], [(10, 12), (5, 5)],
                         'the connect and read timeouts of the fallback request: at most the time left')

    def test_each_request_has_its_own_retries(self):
        """Retries 1: the first request gets HTTP 429, its retry 400 (the file); the fallback request gets 429 too
        and is retried as well, after 3 s again, and answered."""
        set_ai(self.channel, {'ai_max_retries': 1})
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '然後呢？')
        job, sent, sleep = self.turn(session, message, [status_response(429), status_response(400),
                                                        status_response(429), ok_response('好的')])
        self.assertEqual([bool(file_parts(payload)) for payload in sent], [True, True, False, False])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 3])
        self.assertEqual(job.state, 'done', job.error)
        logs = self.env['llm.api.log'].sudo().search([('job_id', '=', job.id)], order='id')
        self.assertEqual([(log.status, log.http_status, log.retry_count) for log in logs],
                         [('http_error', 400, 1), ('ok', 200, 1)])

    def test_the_deadline_stops_the_second_step(self):
        session = self.session()
        self.post(session, '請看報價單', [('報價單.pdf', PDF, 'application/pdf')])
        self.post(session, '還有這張圖', [('photo.png', TINY_PNG, 'image/png')])

        def refusals(clock, _kwargs):
            clock[0] += 5.6  # the second refusal leaves 0.8 s of the 12
            return status_response(400)
        result, sent = self.call_with_clock(session, refusals, timeout=12)
        self.assertEqual([sorted(part['type'] for part in file_parts(payload)) for payload in sent],
                         [['file', 'image_url'], ['image_url']], 'no third request without any file')
        self.assertIn('HTTP 400', result['error'])
        self.assertEqual(result['left_out'], [], 'no request succeeded: nothing to remember')

    def test_the_fallback_cannot_be_built(self):
        """An unexpected error while building the fallback request: the turn fails as before; the server log
        has the type name and the code frames, never the exception text (contract 3.9)."""
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        self.post(session, '然後呢？')
        Channel = type(self.env['im_livechat.channel'])
        build = Channel._ai_build_request

        def build_but_not_the_fallback(channel, *args, **kwargs):
            if kwargs.get('drop'):
                raise RuntimeError(EXCEPTION_TEXT)
            return build(channel, *args, **kwargs)
        with patch(POST, side_effect=refusing(400)) as post, \
                patch.object(Channel, '_ai_build_request', build_but_not_the_fallback), \
                self.assertLogs(CHANNEL_LOGGER, level='WARNING') as logs:
            result = call_backend(self.channel, session=session)
        self.assertEqual(post.call_count, 1)
        self.assertIn('HTTP 400', result['error'])
        self.assertEqual(result['left_out'], [])
        lines = '\n'.join(logs.output)
        self.assertIn('the file fallback request could not be built: RuntimeError', lines)
        self.assertNotIn(EXCEPTION_TEXT, lines)

    def test_each_fallback_request_is_built_again(self):
        """The fallback request is built again, with its own current time, the same choice of files minus the
        ones it leaves out (contract 3.7)."""
        session = self.session()
        _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '可以再說一次嗎？')
        later = SATURDAY_MORNING.replace(minute=21)
        expected = []
        for moment in (SATURDAY_MORNING, later):
            with patch(CLOCK, return_value=moment):
                expected.append(time_line(session._ai_current_time()))
        self.assertNotEqual(*expected)
        with patch(CLOCK, side_effect=[SATURDAY_MORNING, later]) as clock:
            job, sent, _sleep = self.turn(session, message, refusing(400))
        self.assertEqual(clock.call_count, 2, 'the current time of each request')
        self.assertEqual(job.state, 'done', job.error)
        lines = [split_time_line(payload['messages'][0]['content']) for payload in sent]
        self.assertEqual([line for _system, line in lines], expected)
        self.assertEqual(lines[0][0], lines[1][0], 'the rest of the system message is the same')
        first, second = sent[0]['messages'][1:], sent[1]['messages'][1:]
        self.assertEqual(len(first), len(second))
        for before, after in zip(first, second):
            if '這是什麼？' in self.text_of(before['content']):
                self.assertTrue(file_parts({'messages': [before]}))
                self.assert_link_and_note(self.text_of(after['content']), image)
            else:
                self.assertEqual(before, after, 'the other turns are the same')
        self.assertEqual({key: value for key, value in sent[0].items() if key != 'messages'},
                         {key: value for key, value in sent[1].items() if key != 'messages'})

    def block_on_first_request(self, how, code=400):
        """A backend that answers HTTP ``code`` and, while it answers, makes the AI blocked (``how``: the
        connection loses its model, or a manager turns the AI off); then it would answer 200."""
        connection = self.channel.sudo().ai_assistant_id.connection_id
        calls = []

        def post(*_args, **_kwargs):
            calls.append(1)
            if len(calls) > 1:
                return ok_response('不該送出')
            if how == 'not ready':
                connection.with_context(**{NO_IN_USE_CHECK: True}).model = False
            else:
                self.channel.sudo().ai_enabled = False
            return status_response(code)
        return post

    def assert_blocked_turn(self, how, code=400, text='這是什麼？', files=(('photo.png', TINY_PNG, 'image/png'),)):
        """A turn whose first request is answered with HTTP ``code`` while the AI becomes blocked (``how``):
        no other request, the job skipped, no reply and no error message, nothing remembered."""
        session = self.session()
        self.post(session, text, list(files))
        message, _a = self.post(session, '然後呢？')
        job, sent, sleep = self.turn(session, message, self.block_on_first_request(how, code=code))
        self.assertEqual(len(sent), 1, 'no other request while blocked')
        self.assertEqual(job.state, 'skipped')
        self.assertFalse(job.file_fallback_ids)
        self.assertEqual(self.logs(job), [('http_error', code)], 'the request that was sent has its call log')
        messages = self.env['mail.message'].search([('model', '=', 'discuss.channel'), ('res_id', '=', session.id),
                                                    ('author_id', '=', self.channel._get_bot_partner().id)])
        self.assertFalse(messages.filtered(lambda m: m.message_type == 'comment'), 'no reply, no error message')
        return job, messages.filtered(lambda m: m.message_type == 'notification'), sleep

    def test_the_gate_blocks_a_fallback_request(self):
        """Contract 3.7: before each fallback request the run-time gate of 3.5 is checked again; when it blocks,
        no request is sent: the job is skipped and the conversation gets the notice."""
        job, notices, _sleep = self.assert_blocked_turn('not ready')
        self.assertEqual(job.error, BLOCKED_AT_RUN_NOTE)
        self.assertEqual(len(notices), 1, 'the notice of the blocked period')

    def test_turning_the_ai_off_stops_a_fallback_request(self):
        job, notices, _sleep = self.assert_blocked_turn('turned off')
        self.assertEqual(job.error, 'the AI was turned off before the reply: nothing posted')
        self.assertFalse(notices)

    def test_the_gate_blocks_a_retry(self):
        """The same check before a retry of contract 3.3 (HTTP 429 or 503), after the wait."""
        set_ai(self.channel, {'ai_max_retries': 1})
        job, notices, sleep = self.assert_blocked_turn('not ready', code=429, text='你好', files=())
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3], 'the wait, then the check')
        self.assertEqual(job.error, BLOCKED_AT_RUN_NOTE)
        self.assertEqual(len(notices), 1)

    def test_n8n_is_unchanged(self):
        session = self.session(self.n8n)
        message, _a = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        with patch(POST, side_effect=[status_response(400), status_response(200)]) as post, \
                mute_logger(CHANNEL_LOGGER):
            job._run_job(commit=False)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(job.state, 'failed')
        self.assertFalse(job.file_fallback_ids)


class TestFileFallbackMemory(FileFallbackCase):

    def test_the_conversation_remembers(self):
        session = self.session()
        _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '可以再說一次嗎？')
        first, _sent, _sleep = self.turn(session, message, refusing(400))
        self.assertEqual(first.file_fallback_ids, str(image.id))
        self.assertEqual(session._ai_file_fallback_ids(), {image.id})
        message, _a = self.post(session, '那價格呢？')
        second, sent, _sleep = self.turn(session, message, refusing(400))
        self.assertEqual(len(sent), 1, 'no first failing request')
        self.assertFalse(file_parts(sent[0]))
        self.assert_link_and_note(self.turn_text(sent[0], '這是什麼？'), image)
        self.assertEqual(second.state, 'done', second.error)
        self.assertEqual(self.logs(second), [('ok', 200)])
        self.assertFalse(second.file_fallback_ids, 'nothing new was left out')
        self.assertEqual(self.bot_bodies(session), ['<p>好的</p>', '<p>好的</p>'])
        # another file of the same conversation is still sent
        message, _a = self.post(session, '這張呢？', [('other.png', OTHER_PNG, 'image/png')])
        _third, sent, _sleep = self.turn(session, message, [ok_response('好的')])
        self.assertEqual(len(file_parts(sent[0])), 1)

    def test_death_during_the_fallback_request(self):
        """The process dies while the request without the file is out: nothing succeeded, nothing is
        remembered, so the next run of the job (recovery) sends the file first again."""
        session = self.session()
        _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '可以再說一次嗎？')
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        self.kill(job, [status_response(400), _WorkerKilled()])
        self.assertEqual((job.state, job.attempts, job.file_fallback_ids), ('running', 1, False))
        with patch(POST, side_effect=refusing(400)) as post, mute_logger(CHANNEL_LOGGER):
            job._run_job(commit=False)
        sent = [json.loads(call.kwargs['data']) for call in post.call_args_list]
        self.assertEqual([bool(file_parts(payload)) for payload in sent], [True, False])
        self.assertEqual((job.state, job.attempts, job.file_fallback_ids), ('done', 2, str(image.id)), job.error)

    def test_death_after_the_successful_fallback(self):
        """The memory is committed right after the successful request, before the reply is posted: when the
        process dies during the post, the next run of the job sends the file as its link from the start."""
        session = self.session()
        _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '可以再說一次嗎？')
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        Channel = type(self.env['im_livechat.channel'])
        self.kill(job, refusing(400), patch.object(Channel, '_create_bot_message', side_effect=_WorkerKilled()))
        self.assertEqual((job.state, job.attempts, job.file_fallback_ids), ('running', 1, str(image.id)))
        self.assertFalse(self.bot_bodies(session), 'nothing posted yet')
        with patch(POST, side_effect=refusing(400)) as post, mute_logger(CHANNEL_LOGGER):
            job._run_job(commit=False)
        self.assertEqual(post.call_count, 1)
        payload = json.loads(post.call_args.kwargs['data'])
        self.assertFalse(file_parts(payload))
        self.assert_link_and_note(self.turn_text(payload, '這是什麼？'), image)
        self.assertEqual((job.state, job.attempts), ('done', 2), job.error)
        self.assertEqual(self.bot_bodies(session), ['<p>好的</p>'])

    def test_memory_is_the_union_of_the_jobs(self):
        """Each job keeps what its own turn left out; every later request reads the union of the conversation's
        jobs."""
        session = self.session()
        _m, (first_pdf,) = self.post(session, '第一份', [('a.pdf', PDF, 'application/pdf')])
        message, _a = self.post(session, '看得到嗎？')
        first, _sent, _sleep = self.turn(session, message, refusing(400))
        _m, (second_pdf,) = self.post(session, '第二份', [('b.pdf', OTHER_PDF, 'application/pdf')])
        message, _a = self.post(session, '這份呢？')
        second, sent, _sleep = self.turn(session, message, refusing(400))
        self.assertEqual(len(sent), 2, 'the second document is refused first, the first one goes as a link')
        self.assert_link_and_note(self.turn_text(sent[0], '第一份'), first_pdf)
        self.assertEqual((first.file_fallback_ids, second.file_fallback_ids), (str(first_pdf.id), str(second_pdf.id)))
        self.assertEqual(session._ai_file_fallback_ids(), {first_pdf.id, second_pdf.id})
        message, _a = self.post(session, '謝謝')
        _third, sent, _sleep = self.turn(session, message, refusing(400))
        self.assertEqual(len(sent), 1)
        self.assertFalse(file_parts(sent[0]))
        self.assert_link_and_note(self.turn_text(sent[0], '第一份'), first_pdf)
        self.assert_link_and_note(self.turn_text(sent[0], '第二份'), second_pdf)

    def test_memory_lasts_as_long_as_the_conversation(self):
        """The daily cleanup keeps the jobs that remember files (the memory lasts as long as the conversation,
        contract 3.7); a new connection or model does not clear it; deleting those jobs does (README)."""
        session = self.session()
        _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '可以再說一次嗎？')
        remembering, _sent, _sleep = self.turn(session, message, refusing(400))
        message, _a = self.post(session, '謝謝')
        plain, _sent, _sleep = self.turn(session, message, [ok_response('不客氣')])
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_ai_job SET create_date = now() - interval '40 days' WHERE id IN %s",
                            ((remembering.id, plain.id),))
        self.env.invalidate_all()
        self.env['llm.api.log']._cleanup_old_logs()
        self.assertTrue(remembering.exists())
        self.assertFalse(plain.exists())
        set_ai(self.channel, {'ai_model': 'another/model'})
        self.assertEqual(session._ai_file_fallback_ids(), {image.id})
        remembering.unlink()
        self.assertEqual(session._ai_file_fallback_ids(), set())

    def test_forget_files_sent_as_links(self):
        """The button of the reply-job form (system administrators only): the whole conversation forgets the
        files it sends as links only, the next request sends them as file parts again; other conversations keep
        their memory."""
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '看得到嗎？')
        first, _sent, _sleep = self.turn(session, message, refusing(400))
        self.post(session, '第二份', [('b.pdf', OTHER_PDF, 'application/pdf')])
        message, _a = self.post(session, '這份呢？')
        second, _sent, _sleep = self.turn(session, message, refusing(400))
        other_session = self.session()
        _m, (other_image,) = self.post(other_session, '別的對話', [('o.png', TINY_PNG, 'image/png')])
        message, _a = self.post(other_session, '然後呢？')
        other, _sent, _sleep = self.turn(other_session, message, refusing(400))
        self.assertTrue(first.file_fallback_ids and second.file_fallback_ids and other.file_fallback_ids)
        manager = new_test_user(self.env, login='ff_manager',
                                groups='base.group_user,im_livechat.im_livechat_group_manager')
        with self.assertRaises(AccessError):
            first.with_user(manager).action_forget_link_only_attachments()
        admin = new_test_user(self.env, login='ff_admin', groups='base.group_user,base.group_system')
        self.assertIs(first.with_user(admin).action_forget_link_only_attachments(), True)
        self.env.invalidate_all()
        self.assertEqual((first.file_fallback_ids, second.file_fallback_ids), (False, False))
        self.assertEqual(session._ai_file_fallback_ids(), set())
        self.assertEqual(other.file_fallback_ids, str(other_image.id), 'another conversation keeps its memory')
        message, _a = self.post(session, '再看一次')
        _third, sent, _sleep = self.turn(session, message, [ok_response('好的')])
        self.assertEqual([part['type'] for part in file_parts(sent[0])], ['file'], 'the document is a file part again')

    def test_forget_button_on_the_job_form(self):
        form = etree.fromstring(self.env.ref('im_livechat_ai.im_livechat_ai_job_view_form').arch)
        (button,) = form.xpath("//button[@name='action_forget_link_only_attachments']")
        self.assertEqual((button.get('string'), button.get('invisible'), button.get('groups')),
                         ('Forget Files Sent as Links', 'not file_fallback_ids', 'base.group_system'))

    def test_left_out_files_take_no_slot(self):
        """A remembered image is a link with the note and leaves its place in the count to another one."""
        session = self.session()
        _m, images = self.post(session, '五張圖', [('p%s.png' % i, TINY_PNG, 'image/png') for i in range(5)])
        history = session._ai_conversation(self.channel)['history']
        plan = session._ai_attachment_plan(history, 'file', left_out={images[0].id})
        position = len(history) - 1
        self.assertEqual(plan[(position, images[0].id)], {'note': '[附件說明] p0.png：' + NOTE})
        self.assertEqual([bool(plan[(position, image.id)].get('image')) for image in images[1:]], [True] * 4)

    def test_ir_default_never_fills_the_memory(self):
        """Contract 3.5: the job row is created with every field given, the memory too (global ir.default)."""
        self.env['ir.default'].sudo().set('im_livechat.ai.job', 'file_fallback_ids', '1,2,3')
        session = self.session()
        message, _a = self.post(session, '你好')
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        job.invalidate_recordset()
        self.assertFalse(job.file_fallback_ids)
        self.assertEqual(session._ai_file_fallback_ids(), set())

    def test_memory_text(self):
        self.assertEqual(dc.attachment_ids_of(' 12, 7,x,,٣,3 '), {12, 7, 3})
        self.assertEqual(dc.attachment_ids_text({12, 7, 3}), '3,7,12')
        self.assertEqual(dc.attachment_ids_of(False), set())
