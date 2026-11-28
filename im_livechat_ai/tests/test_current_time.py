# -*- coding: utf-8 -*-
"""Contract v1.3: the current time in every request (CONTRACT.md 3.6, 3.7; 18.0.2.1.0).

The staging bench (2026-11-28): nothing in a request said what time it was, and a model told a customer on a
Saturday morning that it was Wednesday afternoon. Every request now carries the current time, computed once
when it is built: n8n gets ``now`` ({utc, local, tz, weekday}); LiteLLM gets one line that ends the system
message, after Live Chat's surface text (the prompt, the event notes, the handoff line; Live Chat has no
identity block). The time zone is the current company's partner's, else the visitor's (a guest's timezone,
else the user's tz), else UTC; a name that is not a known IANA time zone counts as none. The module's own clock
(tools/backend.py utc_now) is frozen here. All HTTP is mocked.
"""

import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.im_livechat_ai.models.discuss_channel import HANDOFF_SYSTEM_LINE, OPERATOR_SYSTEM_NOTE

from .common import (CLOCK, SATURDAY_MORNING, TEST_BACKEND, TEST_N8N_BACKEND, make_ai_channel, ok_response,
                     run_ai_job, sent_json, split_time_line)
from .test_backend import _json_response

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
TAIPEI = {'utc': '2026-11-28T02:20:33Z', 'local': '2026-11-28T10:20:33+08:00', 'tz': 'Asia/Taipei',
          'weekday': 'Saturday'}
TAIPEI_LINE = 'Current time: 2026-11-28 10:20 (Saturday), time zone Asia/Taipei (UTC+08:00).'


class TestCurrentTime(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator_user = new_test_user(cls.env, login='ct_operator', name='客服小明',
                                          groups='base.group_user,im_livechat.im_livechat_group_user')
        common = {'ai_enabled': True, 'ai_max_retries': 0, 'ai_system_prompt': '你是官網客服。',
                  'user_ids': [(6, 0, cls.operator_user.ids)]}
        cls.channel = make_ai_channel(cls.env, {**TEST_BACKEND, **common, 'name': '官網客服'})
        cls.n8n = make_ai_channel(cls.env, {**TEST_N8N_BACKEND, **common, 'name': 'n8n 客服'})
        cls.company_partner = cls.env.company.partner_id
        cls.company_partner.tz = False

    def new_session(self, channel=None, guest_tz=False, user=None):
        """A session whose visitor is a guest (with ``guest_tz``) or ``user``, with one visitor message."""
        channel = channel or self.channel
        session = self.env['discuss.channel'].create({
            'name': 'Time Session', 'channel_type': 'livechat',
            'livechat_operator_id': self.operator_user.partner_id.id, 'livechat_channel_id': channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator_user.partner_id.id})],
        })
        author = {}
        if user:
            self.env['discuss.channel.member'].create({'channel_id': session.id, 'partner_id': user.partner_id.id})
            author['author_id'] = user.partner_id.id
        else:
            guest = self.env['mail.guest'].create({'name': '訪客', 'timezone': guest_tz})
            self.env['discuss.channel.member'].create({'channel_id': session.id, 'guest_id': guest.id})
            author.update(author_id=False, author_guest_id=guest.id)
        self.env['mail.message'].create({'body': '現在還有營業嗎？', 'model': 'discuss.channel', 'res_id': session.id,
                                         'message_type': 'comment', **author})
        return session

    def now_of(self, session):
        with patch(CLOCK, return_value=SATURDAY_MORNING):
            return session._ai_current_time()

    # --- the time zone ---

    def test_company_time_zone_wins(self):
        self.company_partner.tz = 'Asia/Taipei'
        self.assertEqual(self.now_of(self.new_session(guest_tz='America/New_York')), TAIPEI)

    def test_visitor_time_zone_without_a_company_one(self):
        self.assertEqual(self.now_of(self.new_session(guest_tz='America/New_York')), {
            'utc': '2026-11-28T02:20:33Z', 'local': '2026-11-27T21:20:33-05:00', 'tz': 'America/New_York',
            'weekday': 'Friday'})
        portal = new_test_user(self.env, login='ct_portal', name='王小明', groups='base.group_portal',
                               tz='Asia/Kolkata')
        self.assertEqual(self.now_of(self.new_session(user=portal)), {
            'utc': '2026-11-28T02:20:33Z', 'local': '2026-11-28T07:50:33+05:30', 'tz': 'Asia/Kolkata',
            'weekday': 'Saturday'})

    def test_utc_without_any_time_zone(self):
        self.assertEqual(self.now_of(self.new_session()), {
            'utc': '2026-11-28T02:20:33Z', 'local': '2026-11-28T02:20:33+00:00', 'tz': 'UTC',
            'weekday': 'Saturday'})

    def test_unknown_time_zone_counts_as_none(self):
        """A stored name that is not a known IANA time zone (written past the selection, e.g. by an old
        import) is skipped: the next source is used."""
        session = self.new_session(guest_tz='Asia/Tokyo')
        guest = session.channel_member_ids.guest_id
        self.env.flush_all()
        self.env.cr.execute("UPDATE res_partner SET tz = 'Mars/Olympus' WHERE id = %s", (self.company_partner.id,))
        self.env.invalidate_all()
        self.assertEqual(self.now_of(session)['tz'], 'Asia/Tokyo')
        self.env.cr.execute("UPDATE mail_guest SET timezone = 'Not/AZone' WHERE id = %s", (guest.id,))
        self.env.invalidate_all()
        now = self.now_of(session)
        self.assertEqual((now['tz'], now['local']), ('UTC', '2026-11-28T02:20:33+00:00'))

    # --- the requests ---

    def test_litellm_time_line_ends_the_system_message(self):
        """One line, after the prompt, the event notes and the handoff line (contract 3.7), from one reading
        of the clock; never in the metadata."""
        self.company_partner.tz = 'Asia/Taipei'
        session = self.new_session(guest_tz='America/New_York')
        self.env['mail.message'].create({'body': '我幫您查一下。', 'model': 'discuss.channel', 'res_id': session.id,
                                         'message_type': 'comment', 'author_id': self.operator_user.partner_id.id})
        self.env['mail.message'].create({'body': '好的', 'model': 'discuss.channel', 'res_id': session.id,
                                         'message_type': 'comment', 'author_id': False,
                                         'author_guest_id': session.channel_member_ids.guest_id.id})
        with patch(POST, return_value=ok_response('現在是週六上午，我們有營業。')) as post, \
                patch(CLOCK, return_value=SATURDAY_MORNING) as clock:
            job = run_ai_job(self.env, session)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(clock.call_count, 1, 'the current time is read once per request')
        payload = sent_json(post.call_args)
        system = payload['messages'][0]
        self.assertEqual(system['role'], 'system')
        self.assertEqual(system['content'].split('\n\n'),
                         ['你是官網客服。', OPERATOR_SYSTEM_NOTE, HANDOFF_SYSTEM_LINE, TAIPEI_LINE])
        self.assertEqual(split_time_line(system['content'])[1], TAIPEI_LINE)
        self.assertEqual(json.dumps(payload, ensure_ascii=False).count('Current time:'), 1)
        self.assertEqual(list(payload), ['model', 'messages', 'user', 'metadata'])
        self.assertNotIn('Current time', json.dumps(payload['metadata'], ensure_ascii=False))
        self.assertFalse([key for key in payload['metadata'] if 'time' in key or key.endswith('now')])

    def test_n8n_now(self):
        self.company_partner.tz = 'Asia/Taipei'
        session = self.new_session(self.n8n, guest_tz='America/New_York')
        with patch(POST, return_value=_json_response({'reply': '有的'})) as post, \
                patch(CLOCK, return_value=SATURDAY_MORNING) as clock:
            job = run_ai_job(self.env, session)
        self.assertEqual(job.state, 'done', job.error)
        self.assertEqual(clock.call_count, 1)
        payload = sent_json(post.call_args)
        self.assertEqual(payload['now'], TAIPEI)
        self.assertEqual(payload['user']['tz'], 'America/New_York', 'the visitor keeps their own time zone')
        self.assertEqual(payload['system_prompt'], '你是官網客服。', 'the time is not part of the prompt')
        self.assertNotIn('Current time', json.dumps(payload, ensure_ascii=False))

    def test_each_request_reads_the_clock(self):
        """The time is the one of the request, not of the session or the job: a later answer gets a later
        time."""
        session = self.new_session(guest_tz='Asia/Taipei')
        lines = []
        for hour in (2, 9):
            with patch(POST, return_value=ok_response('好')) as post, \
                    patch(CLOCK, return_value=SATURDAY_MORNING.replace(hour=hour)):
                self.channel._generate_ai_reply(session, session._ai_conversation(self.channel))
            lines.append(split_time_line(sent_json(post.call_args)['messages'][0]['content'])[1])
        self.assertEqual(lines, [TAIPEI_LINE,
                                 'Current time: 2026-11-28 17:20 (Saturday), time zone Asia/Taipei (UTC+08:00).'])
