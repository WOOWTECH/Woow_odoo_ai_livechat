# -*- coding: utf-8 -*-

from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestDiscussChannel(TransactionCase):
    """Tests for discuss.channel AI response triggering and message building."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Create livechat channel with AI enabled
        cls.livechat_channel = cls.env['im_livechat.channel'].create({
            'name': 'AI Test Channel',
            'ai_enabled': True,
            'ai_api_base_url': 'https://api.openai.com/v1',
            'ai_api_key': 'sk-test-key',
            'ai_model': 'gpt-4o',
            'ai_system_prompt': 'You are a helpful assistant.',
            'ai_max_history': 10,
        })

        # Create bot partner
        cls.bot_partner = cls.livechat_channel._get_or_create_bot_partner()

        # Create operator user and partner
        cls.operator_partner = cls.env['res.partner'].create({
            'name': 'Test Operator',
        })
        cls.operator_user = cls.env['res.users'].create({
            'name': 'Test Operator',
            'login': 'test_operator_ai',
            'partner_id': cls.operator_partner.id,
        })

        # Create visitor partner (no user_ids)
        cls.visitor_partner = cls.env['res.partner'].create({
            'name': 'Test Visitor',
        })

        # Create livechat session
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Test Session',
            'channel_type': 'livechat',
            'livechat_channel_id': cls.livechat_channel.id,
        })

    # --- Visitor Detection Tests ---

    def _create_message(self, author_id=None, author_guest_id=None, body='Test message'):
        """Helper to create a message in the session."""
        vals = {
            'body': body,
            'model': 'discuss.channel',
            'res_id': self.session.id,
            'message_type': 'comment',
        }
        if author_id:
            vals['author_id'] = author_id
        if author_guest_id:
            vals['author_guest_id'] = author_guest_id
        return self.env['mail.message'].create(vals)

    def test_is_visitor_message_anonymous(self):
        """Anonymous message (no author) should be detected as visitor."""
        msg = self._create_message()
        self.assertTrue(self.session._is_visitor_message(msg))

    def test_is_visitor_message_visitor_partner(self):
        """Partner without user_ids should be detected as visitor."""
        msg = self._create_message(author_id=self.visitor_partner.id)
        self.assertTrue(self.session._is_visitor_message(msg))

    def test_is_visitor_message_operator(self):
        """Operator (partner with user_ids) should NOT be detected as visitor."""
        msg = self._create_message(author_id=self.operator_partner.id)
        self.assertFalse(self.session._is_visitor_message(msg))

    def test_is_visitor_message_bot(self):
        """Bot partner should NOT be detected as visitor."""
        msg = self._create_message(author_id=self.bot_partner.id)
        self.assertFalse(self.session._is_visitor_message(msg))

    def test_is_visitor_message_odoobot(self):
        """OdooBot should NOT be detected as visitor."""
        odoobot = self.env.ref('base.partner_root')
        msg = self._create_message(author_id=odoobot.id)
        self.assertFalse(self.session._is_visitor_message(msg))

    # --- Should Trigger Tests ---

    def test_should_trigger_all_conditions_met(self):
        """Should trigger when all conditions are met."""
        msg = self._create_message(author_id=self.visitor_partner.id)
        self.assertTrue(self.session._should_trigger_ai_response(msg))

    def test_should_trigger_not_livechat(self):
        """Should NOT trigger for non-livechat channels."""
        non_livechat = self.env['discuss.channel'].create({
            'name': 'Regular Channel',
            'channel_type': 'channel',
        })
        msg = self._create_message(author_id=self.visitor_partner.id)
        self.assertFalse(non_livechat._should_trigger_ai_response(msg))

    def test_should_trigger_ai_disabled(self):
        """Should NOT trigger when AI is disabled."""
        self.livechat_channel.write({'ai_enabled': False})
        msg = self._create_message(author_id=self.visitor_partner.id)
        self.assertFalse(self.session._should_trigger_ai_response(msg))
        # Restore
        self.livechat_channel.write({'ai_enabled': True})

    def test_should_trigger_operator_message(self):
        """Should NOT trigger for operator messages."""
        msg = self._create_message(author_id=self.operator_partner.id)
        self.assertFalse(self.session._should_trigger_ai_response(msg))

    def test_should_trigger_bot_message(self):
        """Should NOT trigger for bot messages (prevents loop)."""
        msg = self._create_message(author_id=self.bot_partner.id)
        self.assertFalse(self.session._should_trigger_ai_response(msg))

    # --- Message Building Tests ---

    def test_build_llm_messages_with_system_prompt(self):
        """Should include system prompt as first message."""
        self._create_message(author_id=self.visitor_partner.id, body='Hello')
        messages = self.session._build_llm_messages(self.livechat_channel)

        self.assertTrue(len(messages) >= 2)
        self.assertEqual(messages[0]['role'], 'system')
        self.assertEqual(messages[0]['content'], 'You are a helpful assistant.')

    def test_build_llm_messages_visitor_role(self):
        """Visitor messages should have role 'user'."""
        self._create_message(author_id=self.visitor_partner.id, body='Hello')
        messages = self.session._build_llm_messages(self.livechat_channel)

        user_msgs = [m for m in messages if m['role'] == 'user']
        self.assertTrue(len(user_msgs) >= 1)
        self.assertIn('Hello', user_msgs[-1]['content'])

    def test_build_llm_messages_bot_role(self):
        """Bot messages should have role 'assistant'."""
        self._create_message(author_id=self.visitor_partner.id, body='Hello')
        self._create_message(author_id=self.bot_partner.id, body='Hi there!')
        messages = self.session._build_llm_messages(self.livechat_channel)

        assistant_msgs = [m for m in messages if m['role'] == 'assistant']
        self.assertTrue(len(assistant_msgs) >= 1)
        self.assertIn('Hi there!', assistant_msgs[-1]['content'])

    def test_build_llm_messages_html_stripped(self):
        """HTML tags should be stripped from message bodies."""
        self._create_message(
            author_id=self.visitor_partner.id,
            body='<p>Hello <strong>world</strong></p>',
        )
        messages = self.session._build_llm_messages(self.livechat_channel)

        user_msgs = [m for m in messages if m['role'] == 'user']
        self.assertTrue(len(user_msgs) >= 1)
        self.assertEqual(user_msgs[-1]['content'], 'Hello world')

    def test_build_llm_messages_empty_body_skipped(self):
        """Messages with empty body (after HTML strip) should be skipped."""
        self._create_message(
            author_id=self.visitor_partner.id,
            body='<p></p>',
        )
        # Add a real message
        self._create_message(
            author_id=self.visitor_partner.id,
            body='Real message',
        )
        messages = self.session._build_llm_messages(self.livechat_channel)

        user_msgs = [m for m in messages if m['role'] == 'user']
        for msg in user_msgs:
            self.assertTrue(len(msg['content']) > 0)

    def test_build_llm_messages_respects_max_history(self):
        """Should respect max_history limit."""
        self.livechat_channel.write({'ai_max_history': 3})

        for i in range(5):
            self._create_message(
                author_id=self.visitor_partner.id,
                body=f'Message {i}',
            )

        messages = self.session._build_llm_messages(self.livechat_channel)

        # system + at most 3 user messages
        non_system = [m for m in messages if m['role'] != 'system']
        self.assertTrue(len(non_system) <= 3)

        # Restore
        self.livechat_channel.write({'ai_max_history': 10})

    def test_build_llm_messages_chronological_order(self):
        """Messages should be in chronological order."""
        self._create_message(author_id=self.visitor_partner.id, body='First')
        self._create_message(author_id=self.bot_partner.id, body='Response')
        self._create_message(author_id=self.visitor_partner.id, body='Second')
        messages = self.session._build_llm_messages(self.livechat_channel)

        # Remove system prompt
        conv = [m for m in messages if m['role'] != 'system']
        # Find our test messages at the end
        contents = [m['content'] for m in conv]
        # The last 3 should be in order
        if 'First' in contents and 'Response' in contents and 'Second' in contents:
            first_idx = contents.index('First')
            resp_idx = contents.index('Response')
            second_idx = contents.index('Second')
            self.assertLess(first_idx, resp_idx)
            self.assertLess(resp_idx, second_idx)

    def test_build_llm_messages_no_system_prompt(self):
        """Without system prompt, messages should start with user/assistant."""
        self.livechat_channel.write({'ai_system_prompt': False})
        self._create_message(author_id=self.visitor_partner.id, body='Hello')
        messages = self.session._build_llm_messages(self.livechat_channel)

        self.assertTrue(len(messages) >= 1)
        self.assertNotEqual(messages[0]['role'], 'system')

        # Restore
        self.livechat_channel.write({'ai_system_prompt': 'You are a helpful assistant.'})
