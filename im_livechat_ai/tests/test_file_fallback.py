# -*- coding: utf-8 -*-
"""Contract v1.3: the file fallback (CONTRACT.md 3.7, check 15; 18.0.2.1.0).

The staging bench (2026-11-28): one file the LiteLLM route could not read (HTTP 500 for a PDF, 400 and 422 for a
.txt) made every later turn fail, because the files of the last visitor messages go with every request. Now a
LiteLLM request with file parts (inline images, documents in 'file' mode) that is refused with HTTP 400, 413,
415, 422 or 500 is sent again at once: without the file parts of the earlier visitor messages, then without any
file part. A file left out is its ``[附件]`` line and the note
``[附件說明] <name>：模型無法讀取這個檔案，只附下載連結。``; no wait, not counted in the retries, within the
answer's deadline; each request writes its own call log; the answer is the first success. The conversation
remembers the files left out (im_livechat.ai.job.file_fallback_ids), also across a re-run of the job. Other
statuses, requests without files and n8n are handled as before. All HTTP is mocked.
"""

import base64
import json
from unittest.mock import MagicMock, patch

from odoo.tools import mute_logger

from odoo.addons.im_livechat_ai.models import discuss_channel as dc

from .common import call_backend, ok_response, set_ai
from .test_attachments import PDF, POST, TINY_PNG, AttachmentCase

SLEEP = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep'
CHANNEL_LOGGER = 'odoo.addons.im_livechat_ai.models.im_livechat_channel'
NOTE = '模型無法讀取這個檔案，只附下載連結。'
# a second 1x1 PNG with other bytes than TINY_PNG (another color)
OTHER_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')


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
        """No request succeeds: the visitor gets the error message, as before. The file a step left out stays
        remembered (contract 3.7: a file left out is never sent as a file part again in the conversation)."""
        session = self.session()
        _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '然後呢？')
        job, sent, _sleep = self.turn(session, message, [status_response(500), status_response(500)])
        self.assertEqual(len(sent), 2)
        self.assertEqual(job.state, 'failed')
        self.assertIn('HTTP 500', job.error)
        self.assertEqual(self.logs(job), [('http_error', 500), ('http_error', 500)])
        self.assertEqual(len(self.bot_bodies(session)), 1, 'the error message')
        self.assertEqual(job.file_fallback_ids, str(image.id))

    def test_the_deadline_stops_it(self):
        set_ai(self.channel, {'ai_request_timeout': 12})
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        self.post(session, '然後呢？')
        clock = [1000.0]

        def slow_refusal(*_args, **_kwargs):
            clock[0] += 13  # the refusal came after the deadline of the answer
            return status_response(400)
        with patch(POST, side_effect=slow_refusal) as post, mute_logger(CHANNEL_LOGGER), \
                patch('odoo.addons.im_livechat_ai.models.im_livechat_channel.time.time', side_effect=lambda: clock[0]):
            result = call_backend(self.channel, session=session)
        self.assertEqual(post.call_count, 1)
        self.assertIn('HTTP 400', result['error'])

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

    def test_across_a_run_of_the_same_job(self):
        """The process dies after the fallback left the file out: the next run of the job (recovery) sends
        it as a link from the start."""
        session = self.session()
        _m, (image,) = self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        message, _a = self.post(session, '可以再說一次嗎？')
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        # not self.assertRaises: Odoo's rolls back a savepoint, while the runner commits before each request
        # (here: flushes), so the run's claim and the memory stay as a real process death leaves them
        try:
            with patch(POST, side_effect=[status_response(400), _WorkerKilled()]), patch(SLEEP), \
                    mute_logger(CHANNEL_LOGGER):
                job._run_job(commit=False)
        except _WorkerKilled:
            pass
        else:
            self.fail('the run was not killed')
        self.env.invalidate_all()
        self.assertEqual((job.state, job.attempts, job.file_fallback_ids), ('running', 1, str(image.id)))
        with patch(POST, side_effect=refusing(400)) as post, mute_logger(CHANNEL_LOGGER):
            job._run_job(commit=False)
        self.assertEqual(post.call_count, 1)
        payload = json.loads(post.call_args.kwargs['data'])
        self.assertFalse(file_parts(payload))
        self.assert_link_and_note(self.turn_text(payload, '這是什麼？'), image)
        self.assertEqual((job.state, job.attempts), ('done', 2), job.error)

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
