# -*- coding: utf-8 -*-
"""Messages another automation already answered (e.g. a LINE keyword auto-reply).

The LINE bridge posts the visitor's message with the context key
``im_livechat_ai_skip`` when its keyword rules have already replied, and
records its own reply as the company partner. Neither may start an AI reply,
pause the session or hand it off; the next ordinary message is answered as
usual and sees the keyword exchange in its history.
"""
from unittest.mock import patch

from odoo.addons.im_livechat_ai.models.discuss_channel import SKIP_CONTEXT_KEY

from .test_ai_job import AiJobCase, POST


class TestSkipAnsweredElsewhere(AiJobCase):

    def _post(self, body, author, **ctx):
        return self.session.with_context(**ctx).message_post(
            body=body, author_id=author.id, message_type='comment', subtype_xmlid='mail.mt_comment')

    def test_skip_context_records_without_job(self):
        self._post('選單', self.visitor, **{SKIP_CONTEXT_KEY: True})
        self.assertFalse(self._jobs())
        self.assertFalse(self.session.ai_paused_until)

    def test_skip_context_does_not_hand_off(self):
        # A keyword rule answered "找真人" itself: no handoff pause, no notice job.
        self._post('找真人', self.visitor, **{SKIP_CONTEXT_KEY: True})
        self.assertFalse(self._jobs())
        self.assertFalse(self.session.ai_pause_reason)

    def test_company_partner_is_a_bot(self):
        company_partner = self.env.company.partner_id
        self._post('分會資訊：每週四 06:30 開會', company_partner, **{SKIP_CONTEXT_KEY: True})
        self._post('這是公司身分的自動回覆', company_partner)
        self.assertFalse(self._jobs())
        self.assertFalse(self.session.ai_paused_until)
        self.assertEqual(self.session._ai_author_kind(self.session.message_ids[:1]), 'bot')

    @patch(POST)
    def test_next_message_is_answered_with_keyword_exchange_in_history(self, mock_post):
        self._post('分會資訊', self.visitor, **{SKIP_CONTEXT_KEY: True})
        self._post('長慶白金分會每週四 06:30 開會。', self.env.company.partner_id, **{SKIP_CONTEXT_KEY: True})
        self._visitor_post('那要怎麼報名參訪？')
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        history = self.session._ai_conversation(self.channel)['history']
        roles = [(h['role'], h['text']) for h in history]
        self.assertIn(('user', '分會資訊'), roles)
        self.assertIn(('assistant', '長慶白金分會每週四 06:30 開會。'), roles)
        self.assertEqual(roles[-1], ('user', '那要怎麼報名參訪？'))
