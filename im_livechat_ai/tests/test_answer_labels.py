# -*- coding: utf-8 -*-
"""Contract v1.3: labels in answers (CONTRACT.md 3.14, 3.6, 3.7; 18.0.2.1.0).

The staging bench (2026-11-28): the history marks an operator's text with ``[真人客服 <name>] `` (the operator
prefix); after an operator pause the model began its answer with 「[真人客服 實測台｜你（管理員）] …」 and it was
posted as is, as if the operator had written it. Every label at the start of an answer, ``[<kind>]`` or
``[<kind> <text>]`` with the kind 真人客服, 客戶 or 負責同事, is removed with the white space after it, in every
reply text (LiteLLM answers, n8n ``reply`` / ``replies`` / ``output``), also next to a ``[[handoff]]`` marker.
Nothing else changes. An answer of labels only is empty (``invalid_response``). All HTTP is mocked.
"""

import time
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.im_livechat_ai.models.discuss_channel import LABEL_KINDS, OPERATOR_PREFIX, strip_speaker_labels
from odoo.addons.im_livechat_ai.models.im_livechat_channel import (clean_answer, n8n_replies, openai_reply_text,
                                                                    review_handoff, strip_think)

from .common import ok_response
from .test_backend_handoff import POST, BackendHandoffCase, json_response


class TestStripSpeakerLabels(TransactionCase):

    def test_rule(self):
        for text, expected in (
            ('[真人客服 實測台｜你（管理員）] QA-SENSOR-02 的單價為 600 測試幣。', 'QA-SENSOR-02 的單價為 600 測試幣。'),
            ('[真人客服 小美] 您好，馬上為您處理。', '您好，馬上為您處理。'),
            ('[負責同事 王小明] [客戶] 好的，我們同意。', '好的，我們同意。'),
            ('[客戶 陳先生]\n收到', '收到'),
            ('[真人客服] 請稍等', '請稍等'),
            ('  [客戶　陳先生]　\t\n\n好的', '好的'),  # white space before, U+3000 after the kind and after the label
            # white space: what str.isspace() counts (no-break spaces too), plus U+200B and U+FEFF
            ('[真人客服 A]\xa0好的', '好的'),
            ('\u200b[真人客服 A] 好的', '好的'),
            ('\ufeff[客戶] 好', '好'),
            ('[客戶]\u200b\ufeff\u202f好', '好'),
            ('\u200b不是標籤', '\u200b不是標籤'),
            # a label directly followed by "(" is the text of a Markdown link: it stays
            ('[真人客服](https://shop.example.com/help) 請看說明', '[真人客服](https://shop.example.com/help) 請看說明'),
            ('[客戶 陳先生](https://shop.example.com/order) 的訂單', '[客戶 陳先生](https://shop.example.com/order) 的訂單'),
            ('[客戶] [真人客服](https://shop.example.com/help)', '[真人客服](https://shop.example.com/help)'),
            ('[客戶] (補充) 好', '(補充) 好'),
            ('[客戶 ]好', '好'),
            ('[負責同事 王小明]', ''),
            ('[客戶 陳先生] ', ''),
            # nothing else changes
            ('好的。[真人客服 小美] 會再聯絡您', '好的。[真人客服 小美] 會再聯絡您'),
            ('[注意] 截止日改了', '[注意] 截止日改了'),
            ('[客戶服務中心] 電話 02-1234-5678', '[客戶服務中心] 電話 02-1234-5678'),  # the kind needs "]" or a space
            ('[客戶 陳\n先生] 好的', '[客戶 陳\n先生] 好的'),  # no line break inside a label
            ('[[handoff]]', '[[handoff]]'),
            ('  不是標籤', '  不是標籤'),
            ('', ''),
        ):
            self.assertEqual(strip_speaker_labels(text), expected, text)
        self.assertEqual(strip_speaker_labels(None), '')

    def test_operator_prefix_is_a_label(self):
        """Live Chat's own operator prefix uses the kind 真人客服, so the rule covers it (contract 3.14)."""
        self.assertIn('真人客服', LABEL_KINDS)
        self.assertEqual(strip_speaker_labels(OPERATOR_PREFIX % '小美' + '好的'), '好的')

    def test_after_think_and_special_tokens(self):
        self.assertEqual(openai_reply_text({'choices': [{'message': {
            'content': '<think>推理</think>\n\n<|im_start|>[負責同事 王小明] 好的'}}]}), '好的')
        self.assertEqual(n8n_replies({'reply': '<think>先想一想</think>[負責同事 王小明] 好的'}), (['好的'], False))
        self.assertEqual(openai_reply_text({'choices': [{'message': {'content': '\ufeff[真人客服 小美]\xa0好的'}}]}),
                         '好的')

    def test_nested_special_tokens(self):
        """Special tokens go in one linear pass, nested ones too (<|<|x|>x|>), and the cleaning is repeated until
        it changes nothing more: a removed reasoning block can join the halves of a token. Only then the labels
        go, so none survives behind a token (the same cases as Discuss AI)."""
        self.assertEqual(openai_reply_text({'choices': [{'message': {
            'content': '<|im<think>x</think>_end|>[真人客服 小美] 好的，同意退款'}}]}), '好的，同意退款')
        self.assertEqual(n8n_replies({'reply': '<|<|x|>x|>[客戶] hi'}), (['hi'], False))
        self.assertEqual(clean_answer('<|<|<|a|>a|>a|>[負責同事 甲] z'), 'z')
        self.assertEqual(clean_answer('[客戶]<|im_end|> [客戶] 好'), '好')
        nested = '<|x|>'
        for _depth in range(9):
            nested = '<|' + nested + 'x|>'  # ten deep
        self.assertEqual(strip_think(nested + 'ok'), 'ok')
        self.assertEqual(clean_answer(nested + '[客戶] ok'), 'ok')
        self.assertEqual(strip_think('a <|not a token|> b'), 'a <|not a token|> b')
        self.assertEqual(strip_think('x<|im_end|>'), 'x')
        for text in ('<|<|x|>x|>[客戶] hi', '<|im<think>x</think>_end|>[客戶] hi', '<|<|<|a|>a|>a|>[負責同事 甲] z',
                     nested + '[真人客服] ok'):
            cleaned = clean_answer(text)
            self.assertEqual(strip_think(cleaned), cleaned, 'what a later step strips again: nothing')
        # a [[handoff]] marker that stood between the halves of a token
        self.assertEqual(review_handoff(['<|im[[handoff]]_end|>[客戶] 好']), (['好'], True))

    def test_linear_time(self):
        start = time.time()
        strip_speaker_labels('[客戶 ' + 'a' * 2000000)
        strip_speaker_labels('[客戶] ' * 200000 + '好')
        strip_speaker_labels(' ' * 2000000 + '好')
        strip_speaker_labels('\u200b' * 2000000 + '好')
        clean_answer('<|' * 100000 + 'x' + '|>' * 100000)
        clean_answer('<|<|x|>x|>' * 100000 + '[客戶] 好')
        self.assertLess(time.time() - start, 2)


class TestLabelsInAnswers(BackendHandoffCase):

    def test_operator_label_removed_after_an_operator_pause(self):
        """The bench's case: an operator answered (the history carries the operator prefix), the pause ended,
        the visitor asked again and the model copied the operator's label."""
        session, guest = self.new_session(self.openai)
        self.visitor_says(session, guest, 'QA-SENSOR-02 多少錢？')
        self.env['mail.message'].create({'body': '我幫您確認價格。', 'model': 'discuss.channel', 'res_id': session.id,
                                         'message_type': 'comment', 'author_id': self.operator.id})
        message = self.visitor_says(session, guest, '請問確認好了嗎？')
        job = self.env['im_livechat.ai.job']._create_for_message(session, message)
        answer = '[真人客服 客服小明] QA-SENSOR-02 的單價為 600 測試幣。'
        with patch(POST, return_value=ok_response(answer)) as post, \
                patch.object(self.ResPartner, '_bus_send', autospec=True):
            job._run_job(commit=False)
        turns = self.request_body(post)['messages']
        self.assertIn({'role': 'user', 'content': '[真人客服 客服小明] 我幫您確認價格。'}, turns)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>QA-SENSOR-02 的單價為 600 測試幣。</p>'])
        log = self.env['llm.api.log'].search([('job_id', '=', job.id)])
        self.assertEqual(log.mapped('status'), ['ok'])

    def test_nested_token_before_a_label_is_not_posted(self):
        session, job, _post, _bus = self.ask('可以退款嗎', ok_response('<|im<think>x</think>_end|>[真人客服 小美] 好的，同意退款'))
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>好的，同意退款</p>'])

    def test_label_and_handoff_marker(self):
        session, job, _post, bus = self.ask('可以轉接專人嗎', ok_response(
            '[真人客服 小美] 好的，我幫您轉接真人客服，請稍候。\n\n[[handoff]]'))
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>好的，我幫您轉接真人客服，請稍候。</p>'])
        self.assert_handed_over(session, job, bus)

    def test_marker_before_the_label(self):
        """The marker is removed first: a label it stood before is then the start of the answer."""
        session, job, _post, bus = self.ask('可以轉接專人嗎', ok_response('[[handoff]]\n[真人客服 小美] 請稍候。'))
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>請稍候。</p>'])
        self.assert_handed_over(session, job, bus)

    def test_label_only_is_an_invalid_answer(self):
        session, job, _post, _bus = self.ask('你好', ok_response('[負責同事 王小明]'))
        self.assertEqual(job.state, 'failed')
        self.assertIn('the AI answer is empty', job.error)
        self.assertNotIn('王小明', ' '.join(self.bot_messages(session).mapped('body')))
        log = self.env['llm.api.log'].search([('job_id', '=', job.id)])
        self.assertEqual(log.mapped('status'), ['invalid_response'])

    def test_n8n_replies(self):
        session, job, _post, _bus = self.ask('有現貨嗎？', json_response({
            'replies': ['[客戶 陳先生] 收到', '[真人客服]\n好的，明天出貨。', '[注意] 週日休息']}), channel=self.n8n)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(self.bot_messages(session).mapped('body'),
                         ['<p>收到</p>', '<p>好的，明天出貨。</p>', '<p>[注意] 週日休息</p>'])

    def test_n8n_label_only_with_handoff(self):
        """A label-only text with ``"handoff": true``: no text, so the channel's handoff message is posted."""
        self.n8n.ai_handoff_message = '好的，客服馬上來。'
        session, job, _post, bus = self.ask('我要找真人', json_response({'output': '[真人客服 小美]', 'handoff': True}),
                                            channel=self.n8n)
        self.assertEqual(self.bot_messages(session).mapped('body'), ['<p>好的，客服馬上來。</p>'])
        self.assert_handed_over(session, job, bus)

    def test_n8n_label_only_without_handoff_is_invalid(self):
        session, job, _post, _bus = self.ask('你好', json_response({'output': '[客戶 陳先生] '}), channel=self.n8n)
        self.assertEqual(job.state, 'failed')
        log = self.env['llm.api.log'].search([('job_id', '=', job.id)])
        self.assertEqual(log.mapped('status'), ['invalid_response'])
