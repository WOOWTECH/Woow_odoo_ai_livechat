# -*- coding: utf-8 -*-
"""Visitor attachments go to the backend as they are (owner decision 39).

openai: images as image_url data URIs, documents as ``file`` parts in 'file'
mode, signed download links otherwise; limits and the shared data budget;
only visitor messages; n8n attachment objects; the signed download route;
base64 removed from the stored log payload; the upgrade rule for Hermes
channels. All HTTP to the backends is mocked.
"""

import base64
import io
import json
import time
import zipfile
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase, new_test_user
from odoo.tools import mute_logger

from odoo.addons.im_livechat_ai.models import discuss_channel as dc

from .common import TEST_BACKEND, TEST_N8N_BACKEND, call_backend, ok_response
from .test_backend import _json_response, _load_migration

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
DC = 'odoo.addons.im_livechat_ai.models.discuss_channel'
BASE_URL = 'https://shop.example.com'

TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
PDF = (b'%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n'
       b'2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n')
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b'<!DOCTYPE html><html><body><script>alert(1)</script></body></html>'
DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def make_zip(names):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name in names:
            archive.writestr(name, b'content of ' + name.encode())
    return buffer.getvalue()


def make_docx():
    return make_zip(['[Content_Types].xml', '_rels/.rels', 'word/document.xml'])


def big_pdf(size):
    return PDF + b'%' + b'0' * (size - len(PDF) - 1)


class AttachmentCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param('web.base.url', BASE_URL)
        ICP.set_param('database.uuid', '0edfec80-1111-2222-3333-444455556666')
        cls.operator_user = new_test_user(
            cls.env, login='att_operator', name='客服小明',
            groups='base.group_user,im_livechat.im_livechat_group_user',
        )
        cls.channel = cls.env['im_livechat.channel'].create({
            **TEST_BACKEND, 'name': '官網客服', 'ai_enabled': True, 'ai_max_retries': 0,
        })
        cls.n8n = cls.env['im_livechat.channel'].create({
            **TEST_N8N_BACKEND, 'name': 'n8n 客服', 'ai_enabled': True, 'ai_max_retries': 0,
        })

    def session(self, channel=None):
        channel = channel or self.channel
        self.guest = self.env['mail.guest'].create({'name': '訪客 7'})
        session = self.env['discuss.channel'].create({
            'name': 'Attachment Session',
            'channel_type': 'livechat',
            'livechat_operator_id': self.operator_user.partner_id.id,
            'livechat_channel_id': channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator_user.partner_id.id})],
        })
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'guest_id': self.guest.id})
        return session

    def attachment(self, session, name, raw, mimetype=None):
        vals = {'name': name, 'raw': raw, 'res_model': 'discuss.channel', 'res_id': session.id}
        if mimetype:
            vals['mimetype'] = mimetype
        return self.env['ir.attachment'].create(vals)

    def post(self, session, text, files=(), author='visitor'):
        """A message with attachments ((name, raw[, mimetype]) tuples)."""
        attachments = self.env['ir.attachment']
        for file in files:
            attachments |= self.attachment(session, *file)
        vals = {'body': text, 'model': 'discuss.channel', 'res_id': session.id, 'message_type': 'comment',
                'attachment_ids': [(6, 0, attachments.ids)]}
        if author == 'visitor':
            vals.update(author_id=False, author_guest_id=self.guest.id)
        elif author == 'bot':
            vals['author_id'] = session.livechat_channel_id.ai_bot_partner_id.id
        else:
            vals['author_id'] = self.operator_user.partner_id.id
        message = self.env['mail.message'].create(vals)
        return message, attachments

    def messages(self, session, channel=None):
        channel = channel or self.channel
        with patch(POST) as mock_post:
            mock_post.return_value = ok_response('好的')
            call_backend(channel, session=session)
        return mock_post.call_args.kwargs['json']['messages']

    @staticmethod
    def text_of(content):
        if isinstance(content, str):
            return content
        return '\n'.join(part['text'] for part in content if part['type'] == 'text')

    @staticmethod
    def parts(content, kind):
        return [part for part in content if part['type'] == kind] if isinstance(content, list) else []

    def assert_signed_link(self, text, attachment):
        prefix = '%s/im_livechat_ai/attachment/%s/' % (BASE_URL, attachment.id)
        self.assertIn(prefix, text)
        token = text.split(prefix, 1)[1].split()[0]
        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        self.assertTrue(dc.check_token(secret, attachment.id, attachment.checksum, token))
        expires = int(token.split('.')[0])
        self.assertAlmostEqual(expires, time.time() + 3600, delta=30)


class TestOpenAiAttachments(AttachmentCase):

    def test_image_becomes_image_url_data_uri(self):
        session = self.session()
        self.post(session, '這是什麼？', [('photo.png', TINY_PNG, 'image/png')])
        content = self.messages(session)[-1]['content']
        self.assertEqual([part['type'] for part in content], ['text', 'image_url'])
        self.assertEqual(content[0]['text'], '這是什麼？')  # no line for an inlined image
        self.assertEqual(content[1]['image_url']['url'],
                         'data:image/png;base64,' + base64.b64encode(TINY_PNG).decode())

    def test_image_type_from_content_not_name(self):
        session = self.session()
        self.post(session, '', [('photo.jpg', TINY_PNG, 'image/jpeg')])  # a PNG named and typed as JPEG
        content = self.messages(session)[-1]['content']
        # attachment-only visitor message: no text part, the image only
        self.assertEqual(content, [{'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(TINY_PNG).decode()}}])

    def test_image_in_link_mode_still_inlined(self):
        self.channel.ai_document_mode = 'link'
        session = self.session()
        self.post(session, '看圖', [('photo.png', TINY_PNG, 'image/png')])
        self.assertEqual(len(self.parts(self.messages(session)[-1]['content'], 'image_url')), 1)

    def test_pdf_file_mode_is_a_file_part(self):
        session = self.session()
        _m, (pdf,) = self.post(session, '請看報價單', [('報價單.pdf', PDF, 'application/pdf')])
        content = self.messages(session)[-1]['content']
        self.assertEqual([part['type'] for part in content], ['text', 'file'])
        self.assertEqual(content[1], {'type': 'file', 'file': {
            'filename': '報價單.pdf',
            'file_data': 'data:application/pdf;base64,' + base64.b64encode(PDF).decode(),
        }})
        self.assertEqual(content[0]['text'], '請看報價單\n[附件] 報價單.pdf（application/pdf，%s B）：檔案已附在這則訊息'
                         % len(PDF))
        self.assertNotIn('/im_livechat_ai/attachment/', content[0]['text'])
        self.assertEqual(pdf.raw, PDF)

    def test_docx_keeps_original_mimetype_and_bytes(self):
        session = self.session()
        docx = make_docx()
        self.post(session, '合約', [('合約.docx', docx, DOCX_TYPE)])
        (part,) = self.parts(self.messages(session)[-1]['content'], 'file')
        self.assertEqual(part['file']['file_data'], 'data:%s;base64,%s' % (DOCX_TYPE, base64.b64encode(docx).decode()))

    def test_link_mode_gives_a_signed_url_line(self):
        self.channel.ai_document_mode = 'link'
        session = self.session()
        _m, (pdf,) = self.post(session, '請看報價單', [('報價單.pdf', PDF, 'application/pdf')])
        content = self.messages(session)[-1]['content']
        self.assertIsInstance(content, str)
        self.assertTrue(content.startswith('請看報價單\n[附件] 報價單.pdf（application/pdf，%s B）：%s/im_livechat_ai/'
                                           'attachment/%s/' % (len(PDF), BASE_URL, pdf.id)))
        self.assert_signed_link(content, pdf)
        self.assertNotIn('附件說明', content)

    def test_dangerous_types_are_links_even_in_file_mode(self):
        session = self.session()
        self.assertEqual(self.channel.ai_document_mode, 'file')
        _m, attachments = self.post(session, '檔案', [
            ('logo.svg', SVG, 'image/svg+xml'),
            ('page.html', HTML, 'text/html'),
            ('page.txt', HTML, 'text/plain'),  # html content under a harmless name
            ('setup.exe', b'MZ\x90\x00' + b'\x00' * 64, 'application/octet-stream'),
            ('tools.zip', make_zip(['readme.txt', 'run.exe']), 'application/zip'),
            ('script.js', b'alert(1)', 'text/javascript'),
        ])
        content = self.messages(session)[-1]['content']
        self.assertIsInstance(content, str)  # no image or file part at all
        self.assertEqual(content.count('/im_livechat_ai/attachment/'), len(attachments))
        self.assertNotIn('base64', content)
        self.assertNotIn('alert(1)', content)
        self.assertNotIn('附件說明', content)
        for attachment in attachments:
            self.assert_signed_link(content, attachment)

    def test_other_files_are_links(self):
        """Not a document the backend reads (archive, media): a link only."""
        session = self.session()
        self.post(session, '檔案', [('photos.zip', make_zip(['a.txt']), 'application/zip'),
                                   ('clip.mp4', b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 32, 'video/mp4')])
        content = self.messages(session)[-1]['content']
        self.assertIsInstance(content, str)
        self.assertEqual(content.count('/im_livechat_ai/attachment/'), 2)

    def test_file_over_size_limit_is_a_link_with_note(self):
        session = self.session()
        size = dc.INLINE_FILE_MAX_BYTES + 1
        _m, (pdf,) = self.post(session, '大檔', [('big.pdf', big_pdf(size), 'application/pdf')])
        content = self.messages(session)[-1]['content']
        self.assertIsInstance(content, str)
        self.assert_signed_link(content, pdf)
        self.assertIn('[附件說明] big.pdf：檔案太大（5.0 MB，上限 5.0 MB），只附下載連結。', content)

    def test_image_over_size_limit_is_a_link_with_note(self):
        session = self.session()
        raw = TINY_PNG + b'\x00' * (dc.INLINE_FILE_MAX_BYTES + 1)
        self.post(session, '大圖', [('big.png', raw, 'image/png')])
        content = self.messages(session)[-1]['content']
        self.assertIsInstance(content, str)
        self.assertIn('[附件] big.png（image/png，5.0 MB）：%s/im_livechat_ai/attachment/' % BASE_URL, content)
        self.assertIn('[附件說明] big.png：圖片太大', content)

    def test_seven_mb_budget_is_shared_newest_first(self):
        session = self.session()
        size = 4 * 1024 * 1024  # 5.3 MB as base64: two do not fit in 7 MB
        _m, (old,) = self.post(session, '第一份', [('old.pdf', big_pdf(size), 'application/pdf')])
        self.post(session, '第二份', [('new.pdf', big_pdf(size), 'application/pdf'),
                                    ('photo.png', TINY_PNG, 'image/png')])
        messages = self.messages(session)
        last = messages[-1]['content']
        self.assertEqual([part['type'] for part in last], ['text', 'image_url', 'file'])
        self.assertEqual(self.parts(last, 'file')[0]['file']['filename'], 'new.pdf')
        earlier = messages[-2]['content']
        self.assertIsInstance(earlier, str)
        self.assert_signed_link(earlier, old)
        self.assertIn('[附件說明] old.pdf：這次請求的附件總量已達上限（7.0 MB），只附下載連結。', earlier)
        total = sum(len(json.dumps(m['content'])) for m in messages)
        self.assertLess(total, dc.INLINE_DATA_BUDGET + 10000)

    def test_image_count_and_age_limits(self):
        session = self.session()
        _m, (old_image,) = self.post(session, '舊圖', [('old.png', TINY_PNG, 'image/png')])
        for i in range(dc.INLINE_IMAGE_MESSAGES):
            self.post(session, '問題 %s' % i)
        _m, images = self.post(session, '五張圖', [('p%s.png' % i, TINY_PNG, 'image/png') for i in range(5)])
        messages = self.messages(session)
        last = messages[-1]['content']
        self.assertEqual(len(self.parts(last, 'image_url')), dc.INLINE_IMAGE_MAX_COUNT)
        self.assertIn('[附件說明] p4.png：每次最多附上 4 張圖片，這張只附下載連結。', self.text_of(last))
        self.assert_signed_link(self.text_of(last), images[4])
        # Older than the 3 most recent visitor messages: a link, no note.
        old_turn = next(m['content'] for m in messages if '舊圖' in self.text_of(m['content']))
        self.assertIsInstance(old_turn, str)
        self.assert_signed_link(old_turn, old_image)
        self.assertNotIn('附件說明', old_turn)

    def test_document_count_and_age_limits(self):
        session = self.session()
        _m, (old,) = self.post(session, '舊檔', [('old.pdf', PDF, 'application/pdf')])
        for i in range(dc.INLINE_DOC_MESSAGES):
            self.post(session, '問題 %s' % i)
        _m, docs = self.post(session, '六份', [('d%s.pdf' % i, PDF, 'application/pdf') for i in range(6)])
        messages = self.messages(session)
        last = messages[-1]['content']
        self.assertEqual(len(self.parts(last, 'file')), dc.INLINE_DOC_MAX_COUNT)
        self.assertIn('[附件說明] d5.pdf：每次最多附上 5 個檔案，這個只附下載連結。', self.text_of(last))
        self.assert_signed_link(self.text_of(last), docs[5])
        old_turn = next(m['content'] for m in messages if '舊檔' in self.text_of(m['content']))
        self.assertIsInstance(old_turn, str)
        self.assert_signed_link(old_turn, old)

    def test_only_visitor_attachments_are_used(self):
        session = self.session()
        self.post(session, '你好')
        self.post(session, '機器人附件', [('bot.png', TINY_PNG, 'image/png'), ('bot.pdf', PDF, 'application/pdf')],
                  author='bot')
        self.post(session, '', [('bot-only.pdf', PDF, 'application/pdf')], author='bot')
        self.post(session, '客服附件', [('op.pdf', PDF, 'application/pdf')], author='operator')
        self.post(session, '謝謝')
        messages = self.messages(session)
        dump = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn('/im_livechat_ai/attachment/', dump)
        self.assertNotIn('base64', dump)
        for name in ('bot.png', 'bot.pdf', 'bot-only.pdf', 'op.pdf'):
            self.assertNotIn(name, dump)
        self.assertTrue(all(isinstance(m['content'], str) for m in messages))

    def test_payload_log_has_base64_replaced(self):
        self.channel.sudo().ai_log_payload = True
        session = self.session()
        self.post(session, '看檔', [('a.pdf', PDF, 'application/pdf'), ('p.png', TINY_PNG, 'image/png')])
        self.messages(session)
        log = self.env['llm.api.log'].sudo().search([('discuss_channel_id', '=', session.id)], limit=1)
        self.assertTrue(log.request_payload)
        self.assertNotIn(base64.b64encode(PDF).decode(), log.request_payload)
        self.assertNotIn(base64.b64encode(TINY_PNG).decode(), log.request_payload)
        self.assertIn('data:application/pdf;base64,<%s bytes base64 omitted>' % len(base64.b64encode(PDF)),
                      log.request_payload)
        self.assertIn('data:image/png;base64,<%s bytes base64 omitted>' % len(base64.b64encode(TINY_PNG)),
                      log.request_payload)
        self.assertIn('檔案已附在這則訊息', log.request_payload)  # the rest is kept

    def test_redact_helper(self):
        data = {'a': ['x data:text/csv;base64,QUJD y', {'b': 'data:image/png;charset=x;base64,QUJDRA=='}], 'n': 3}
        self.assertEqual(dc.redact_base64(data), {
            'a': ['x data:text/csv;base64,<4 bytes base64 omitted> y',
                  {'b': 'data:image/png;charset=x;base64,<8 bytes base64 omitted>'}], 'n': 3})


class TestN8nAttachments(AttachmentCase):

    def test_payload_carries_signed_urls(self):
        session = self.session(self.n8n)
        _m, (old,) = self.post(session, '第一份', [('old.pdf', PDF, 'application/pdf')])
        self.post(session, '機器人', [('bot.pdf', PDF, 'application/pdf')], author='bot')
        message, (pdf, png) = self.post(session, '', [('報價單.pdf', PDF, 'application/pdf'),
                                                      ('photo.png', TINY_PNG, 'image/png')])
        with patch(POST) as mock_post:
            mock_post.return_value = _json_response({'skip': True})
            self.n8n._generate_ai_reply(session, session._ai_conversation(self.n8n), trigger_message=message)
        body = mock_post.call_args.kwargs['data']
        self.assertNotIn(base64.b64encode(PDF), body)
        self.assertNotIn(b'base64', body)
        payload = json.loads(body)
        self.assertEqual(payload['message']['id'], message.id)
        self.assertEqual(payload['message']['text'], '')
        self.assertEqual(payload['chatInput'], '')
        attachments = payload['message']['attachments']
        self.assertEqual([a['id'] for a in attachments], [pdf.id, png.id])
        first = attachments[0]
        self.assertEqual(set(first), {'id', 'name', 'mimetype', 'size', 'url', 'expires_at'})
        self.assertEqual(first['name'], '報價單.pdf')
        self.assertEqual(first['mimetype'], 'application/pdf')
        self.assertEqual(first['size'], len(PDF))
        self.assertTrue(first['expires_at'].endswith('Z'))
        self.assert_signed_link(first['url'], pdf)
        self.assertEqual(payload['history'], [
            {'role': 'user', 'text': '第一份', 'attachments': [payload['history'][0]['attachments'][0]]},
            {'role': 'assistant', 'text': '機器人', 'attachments': []},  # the bot's file is not sent
        ])
        self.assert_signed_link(payload['history'][0]['attachments'][0]['url'], old)


@tagged('post_install', '-at_install')
class TestAttachmentRoute(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.secret = cls.env['ir.config_parameter'].sudo().get_param('database.secret')
        operator = cls.env.ref('base.partner_admin')
        livechat = cls.env['im_livechat.channel'].create({'name': 'Route 客服'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Route Session', 'channel_type': 'livechat',
            'livechat_operator_id': operator.id, 'livechat_channel_id': livechat.id,
        })
        cls.attachment = cls._message_attachment(cls.session, 'a.html', HTML, 'text/html')
        group = cls.env['discuss.channel'].create({'name': 'Team', 'channel_type': 'channel'})
        cls.team_attachment = cls._message_attachment(group, 'team.pdf', PDF, 'application/pdf')
        cls.partner_attachment = cls.env['ir.attachment'].create({
            'name': 'partner.pdf', 'raw': PDF, 'res_model': 'res.partner', 'res_id': operator.id,
        })

    @classmethod
    def _message_attachment(cls, channel, name, raw, mimetype):
        attachment = cls.env['ir.attachment'].create({
            'name': name, 'raw': raw, 'mimetype': mimetype, 'res_model': 'discuss.channel', 'res_id': channel.id,
        })
        cls.env['mail.message'].create({
            'body': 'file', 'model': 'discuss.channel', 'res_id': channel.id, 'message_type': 'comment',
            'author_id': False, 'attachment_ids': [(6, 0, attachment.ids)],
        })
        return attachment

    def url(self, attachment, token=None, now=None):
        if token is None:
            token, _expires = dc.make_token(self.secret, attachment.id, attachment.checksum, now=now)
        return '/im_livechat_ai/attachment/%s/%s' % (attachment.id, token)

    def test_valid_link_is_a_download(self):
        response = self.url_open(self.url(self.attachment))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, HTML)
        self.assertTrue(response.headers['Content-Disposition'].startswith('attachment'))
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_url_built_by_the_module_works(self):
        info = self.env['discuss.channel']._ai_attachment_info(self.attachment, base_url='')
        response = self.url_open(info['url'])
        self.assertEqual(response.status_code, 200)

    @mute_logger('odoo.http')
    def test_refused_links_are_404(self):
        token, expires = dc.make_token(self.secret, self.attachment.id, self.attachment.checksum)
        signature = token.split('.', 1)[1]
        bad = []
        # expired (signed two hours ago, valid one hour)
        bad.append(self.url(self.attachment, now=time.time() - 7200))
        # wrong signature, signature of another attachment, a changed expiry
        bad.append(self.url(self.attachment, token='%s.%s' % (expires, '0' * 64)))
        other, _e = dc.make_token(self.secret, self.team_attachment.id, self.team_attachment.checksum)
        bad.append(self.url(self.attachment, token=other))
        bad.append(self.url(self.attachment, token='%s.%s' % (expires + 3600, signature)))
        bad.append(self.url(self.attachment, token='garbage'))
        # signed correctly, but not an attachment of a live chat session
        bad.append(self.url(self.team_attachment))
        bad.append(self.url(self.partner_attachment))
        # does not exist
        bad.append('/im_livechat_ai/attachment/999999999/%s' % token)
        for url in bad:
            with self.subTest(url=url):
                self.assertEqual(self.url_open(url).status_code, 404)


class TestDocumentModeUpgrade(TransactionCase):

    def test_hermes_channels_get_link(self):
        post = _load_migration('18.0.1.2.0', 'post-migrate')
        Channel = self.env['im_livechat.channel']
        by_model = Channel.create({'name': 'Hermes model', 'ai_model': 'hermes-agent'})
        by_url = Channel.create({'name': 'Hermes URL', 'ai_endpoint_url':
                                 'http://hermes.agents.svc.cluster.local:8642/v1'})
        litellm = Channel.create({'name': 'LiteLLM', 'ai_model': 'chatgpt-pool/gpt-6-sol',
                                  'ai_endpoint_url': 'https://litellm.example.com/v1'})
        already = Channel.create({'name': 'Chosen', 'ai_model': 'hermes-agent', 'ai_document_mode': 'file'})
        # pre-migrate created the column empty: no channel has a value yet
        self.env.flush_all()
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_document_mode = NULL WHERE id IN %s",
                            (tuple((by_model | by_url | litellm).ids),))
        self.env.invalidate_all()
        with mute_logger(post.__name__):
            post.migrate(self.env.cr, '18.0.1.1.0')
            post.migrate(self.env.cr, '18.0.1.1.0')  # idempotent
        self.env.invalidate_all()
        self.assertEqual(by_model.ai_document_mode, 'link')
        self.assertEqual(by_url.ai_document_mode, 'link')
        self.assertEqual(litellm.ai_document_mode, 'file')
        self.assertEqual(already.ai_document_mode, 'file')  # a value already set is kept

    def test_pre_migrate_creates_the_column_empty(self):
        pre = _load_migration('18.0.1.2.0', 'pre-migrate')
        channel = self.env['im_livechat.channel'].create({'name': 'Pre'})
        self.env.flush_all()
        self.env.cr.execute("ALTER TABLE im_livechat_channel DROP COLUMN ai_document_mode")
        pre.migrate(self.env.cr, '18.0.1.1.0')
        self.env.cr.execute("SELECT ai_document_mode FROM im_livechat_channel WHERE id = %s", (channel.id,))
        self.assertIsNone(self.env.cr.fetchone()[0])

    def test_new_channel_default_is_file(self):
        self.assertEqual(self.env['im_livechat.channel'].create({'name': 'Neu'}).ai_document_mode, 'file')
