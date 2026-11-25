# -*- coding: utf-8 -*-
"""Hardening from the PR #9 review: linear think stripping and capped
formatting (#16), the signed link's expiry (#21), the bot set only when a
message has an author (#25), and person names by the contract's rules
(3.6, 3.7, 3.14). No real key, no HTTP."""

import random
import re
import time
from unittest.mock import patch

from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.im_livechat_ai.models import discuss_channel as dc
from odoo.addons.im_livechat_ai.models import im_livechat_channel as ch
from odoo.addons.im_livechat_ai.tools.backend import one_line_name, person_name
from odoo.addons.im_livechat_ai.tools.markdown import markdown_to_html

from .common import TEST_BACKEND, make_ai_channel


class TestLinearText(TransactionCase):

    def test_think_pairs_match_the_pattern(self):
        rng = random.Random(1125)
        tokens = ['<think>', '</think>', '<thinking>', '</thinking >', '<THINK>', '</Think>', 'a', ' ', '\n',
                  '<think', '>', '</thinking>']
        for _i in range(3000):
            text = ''.join(rng.choice(tokens) for _j in range(rng.randint(0, 12)))
            self.assertEqual(ch._strip_think_pairs(text), ch._THINK_RE.sub('', text), repr(text))

    def test_adversarial_answers_stay_fast(self):
        cases = ['**a ' * 25000, '*a ' * 33000, '_a ' * 33000, '~~a ' * 25000, '# ' + '#' * 16000 + 'x']
        for text in cases:
            start = time.time()
            markdown_to_html(text, link_hosts=())
            self.assertLess(time.time() - start, 2, text[:8])
        start = time.time()
        self.assertEqual(ch.strip_think('<think>a' * 16000), '')
        self.assertLess(time.time() - start, 2)

    def test_formatting_is_kept_for_normal_lines(self):
        html = str(markdown_to_html('**粗體** 與 *斜體* 與 ~~刪除~~\n# 標題', link_hosts=()))
        for tag in ('<strong>粗體</strong>', '<i>斜體</i>', '<del>刪除</del>', '<h4>標題</h4>'):
            self.assertIn(tag, html)


class TestSignedLink(TransactionCase):

    def test_expiry_must_be_ascii_digits(self):
        for expires in ('²', '①', '٣', ''):
            self.assertFalse(dc.check_token('secret', 1, 'checksum', '%s.abc' % expires))

    def test_times_are_iso_8601_utc_without_fractions(self):
        self.assertRegex(dc.iso_utc(1793433600.75), r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


class TestNames(TransactionCase):

    def test_name_rules(self):
        self.assertEqual(person_name('boss@example.com'), '')
        self.assertEqual(person_name('老闆] [負責同事'), '老闆] [負責同事', 'as stored')
        self.assertEqual(one_line_name('Mallory \n - account: [admin]'), 'Mallory - account: (admin)')
        self.assertEqual(one_line_name('x' * 100), 'x' * 80)
        self.assertEqual(one_line_name('a@example.com'), '')

    def test_operator_prefix(self):
        named = new_test_user(self.env, login='op_named', name='小美 [主管]', groups='base.group_user')
        address = new_test_user(self.env, login='op_address', name='op@example.com', groups='base.group_user')
        Session = self.env['discuss.channel']
        self.assertEqual(Session._ai_operator_prefix(named.partner_id), '[真人客服 小美 (主管)] ')
        self.assertEqual(Session._ai_operator_prefix(address.partner_id), '[真人客服] ')

    def test_visitor_named_after_an_address(self):
        channel = make_ai_channel(self.env, {**TEST_BACKEND, 'name': 'Names', 'ai_enabled': True})
        visitor = self.env['res.partner'].create({'name': 'boss@example.com', 'email': 'boss@example.com'})
        session = self.env['discuss.channel'].create({
            'name': 'Names', 'channel_type': 'livechat', 'livechat_channel_id': channel.id,
            'livechat_operator_id': self.env.ref('base.partner_admin').id,
            'channel_member_ids': [(0, 0, {'partner_id': self.env.ref('base.partner_admin').id})],
        })
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'partner_id': visitor.id})
        identity = session._ai_visitor_identity()
        self.assertEqual(identity['name'], '')
        self.assertEqual(identity['email'], 'boss@example.com', 'n8n still gets the email')
        metadata = channel._ai_openai_metadata('woow-ai-x-1', identity, channel.name)
        self.assertNotIn('odoo_name', metadata)
        self.assertFalse([value for value in metadata.values() if '@' in value])


class TestBotSetOnlyForAuthors(TransactionCase):

    def test_guest_messages_skip_the_bot_set(self):
        channel = make_ai_channel(self.env, {**TEST_BACKEND, 'name': 'Bots', 'ai_enabled': True})
        guest = self.env['mail.guest'].create({'name': 'Guest'})
        session = self.env['discuss.channel'].create({
            'name': 'Bots', 'channel_type': 'livechat', 'livechat_channel_id': channel.id,
            'livechat_operator_id': self.env.ref('base.partner_admin').id,
        })
        message = self.env['mail.message'].create({
            'body': 'hi', 'model': 'discuss.channel', 'res_id': session.id, 'message_type': 'comment',
            'author_id': False, 'author_guest_id': guest.id})
        Session = type(self.env['discuss.channel'])
        with patch.object(Session, '_ai_bot_ids', side_effect=AssertionError('computed for a guest')):
            self.assertEqual(session._ai_author_kind(message), dc.AUTHOR_VISITOR)
        bot_message = self.env['mail.message'].create({
            'body': 'bot', 'model': 'discuss.channel', 'res_id': session.id, 'message_type': 'comment',
            'author_id': channel._get_bot_partner().id})
        self.assertEqual(session._ai_author_kind(bot_message), dc.AUTHOR_BOT)

    def test_operator_marker_pattern_still_matches(self):
        """The operator prefix keeps its shape for named operators (n8n workflows may read it)."""
        self.assertTrue(re.match(r'^\[真人客服 .+\] $', dc.OPERATOR_PREFIX % '小美'))
