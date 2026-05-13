# -*- coding: utf-8 -*-
"""
Integration tests for im_livechat_n8n + woow_odoo_livechat_line combined functionality.

These tests validate the complete message flow when both modules coexist:
  LINE User → LINE Webhook → Odoo Livechat → n8n Webhook → LLM → n8n Callback → Odoo → LINE Push

Test categories:
  1. Loop prevention (no infinite message echoes)
  2. Cross-module message routing (correct forwarding decisions)
  3. Author type detection compatibility
  4. Webhook security boundary
  5. Edge cases & error resilience
  6. Configuration combinations
"""

from unittest.mock import patch, MagicMock, PropertyMock
from datetime import timedelta
import json
import re

from odoo.tests import TransactionCase, tagged, HttpCase
from odoo import fields, Command
from odoo.exceptions import UserError


# ---------------------------------------------------------------------------
# Helper: check if LINE module is installed
# ---------------------------------------------------------------------------
def _line_module_installed(env):
    """Return True if woow_odoo_livechat_line is installed."""
    mod = env['ir.module.module'].sudo().search(
        [('name', '=', 'woow_odoo_livechat_line'), ('state', '=', 'installed')],
        limit=1,
    )
    return bool(mod)


def _create_livechat_session(env, livechat_channel, uuid_val, name='Test Session',
                             operator_partner=None, line_user_id=None):
    """Create a livechat-type discuss.channel with required operator.

    Odoo 18 enforces a DB constraint: livechat channels MUST have
    livechat_operator_id set. This helper handles that.
    """
    if not operator_partner:
        operator_partner = env.ref('base.partner_admin')

    vals = {
        'name': name,
        'channel_type': 'livechat',
        'livechat_channel_id': livechat_channel.id,
        'uuid': uuid_val,
        'livechat_operator_id': operator_partner.id,
        'channel_member_ids': [
            Command.create({'partner_id': operator_partner.id}),
        ],
    }
    session = env['discuss.channel'].create(vals)

    if line_user_id and _line_module_installed(env):
        session.write({'line_user_id': line_user_id})

    return session


# ===================================================================
# 1. LOOP PREVENTION TESTS
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestLoopPrevention(TransactionCase):
    """Verify that messages do not create infinite loops between modules.

    Critical scenarios:
    - n8n bot reply must NOT re-trigger n8n webhook
    - n8n bot reply SHOULD be forwarded to LINE (if applicable)
    - LINE inbound message must NOT echo back to LINE
    - LINE inbound message SHOULD trigger n8n webhook
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Livechat channel with n8n enabled
        cls.livechat_channel = cls.env['im_livechat.channel'].create({
            'name': 'LINE+N8N Test Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/test',
        })

        # Operator user + partner
        cls.operator_user = cls.env['res.users'].create({
            'name': 'Test Operator',
            'login': 'test_op_loop',
        })
        cls.operator_partner = cls.operator_user.partner_id

        # Livechat session
        cls.session = _create_livechat_session(
            cls.env, cls.livechat_channel, 'loop-test-uuid-001',
            name='Test LINE Session',
            operator_partner=cls.operator_partner,
            line_user_id='U_test_line_user_001',
        )

        # Visitor partner (no user_ids → treated as visitor by n8n module)
        cls.visitor_partner = cls.env['res.partner'].create({
            'name': 'LINE Visitor',
        })

        # n8n bot partner
        cls.n8n_bot = cls.env.ref(
            'im_livechat_n8n.partner_n8n_bot', raise_if_not_found=False
        )

        # Mail guest (simulates LINE visitor in Odoo)
        cls.guest = cls.env['mail.guest'].create({
            'name': 'LINE Guest #1',
        })

    # ------------------------------------------------------------------
    # 1.1 n8n bot reply must NOT re-trigger n8n outbound webhook
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_n8n_bot_message_does_not_retrigger_webhook(self, mock_trigger):
        """n8n bot partner posting should NOT trigger outbound webhook."""
        self.assertTrue(self.n8n_bot, "n8n bot partner must exist")

        msg = self.session.message_post(
            body='<p>AI response from n8n</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.n8n_bot.id,
        )

        # _is_visitor_message should return False for n8n bot
        self.assertFalse(
            self.session._is_visitor_message(msg),
            "n8n bot messages must NOT be classified as visitor messages"
        )
        # Webhook must not fire
        mock_trigger.assert_not_called()

    # ------------------------------------------------------------------
    # 1.2 n8n bot reply SHOULD pass LINE module's forwarding check
    # ------------------------------------------------------------------
    def test_n8n_bot_message_passes_line_forwarding_check(self):
        """n8n bot messages satisfy LINE module's outbound conditions.

        LINE module checks: not author_guest_id AND author_id AND message_type == 'comment'
        n8n bot has: author_id = partner, author_guest_id = NULL, message_type = 'comment'
        → Should pass the check and be forwarded to LINE.
        """
        self.assertTrue(self.n8n_bot, "n8n bot partner must exist")

        msg = self.env['mail.message'].create({
            'body': '<p>Bot reply for LINE</p>',
            'author_id': self.n8n_bot.id,
            'message_type': 'comment',
            'model': 'discuss.channel',
            'res_id': self.session.id,
        })

        # LINE module's check: not author_guest_id AND author_id AND message_type == 'comment'
        self.assertFalse(msg.author_guest_id, "n8n bot has no guest_id")
        self.assertTrue(msg.author_id, "n8n bot has author_id")
        self.assertEqual(msg.message_type, 'comment')
        # All three conditions pass → LINE module WOULD forward this

    # ------------------------------------------------------------------
    # 1.3 LINE inbound message (via context flag) must NOT echo to LINE
    # ------------------------------------------------------------------
    def test_line_inbound_context_flag_prevents_echo(self):
        """Messages with from_line_webhook=True must not be re-sent to LINE."""
        if not _line_module_installed(self.env):
            self.skipTest("LINE module not installed")

        with patch.object(
            type(self.env['mail.message']),
            '_send_to_line_if_applicable',
            wraps=self.env['mail.message']._send_to_line_if_applicable
        ) as mock_send:
            self.session.with_context(
                from_line_webhook=True,
            ).message_post(
                body='<p>Message from LINE</p>',
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
                author_guest_id=self.guest.id,
            )
            mock_send.assert_not_called()

    # ------------------------------------------------------------------
    # 1.4 LINE guest message SHOULD trigger n8n webhook
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_line_guest_message_triggers_n8n_webhook(self, mock_trigger):
        """LINE visitor message should trigger n8n webhook.

        In the real LINE flow, visitor messages are posted with author_id set
        to a visitor partner (no user_ids).  n8n's _is_visitor_message checks
        author_id.user_ids: empty → returns True → webhook fires.
        """
        msg = self.session.with_context(
            from_line_webhook=True,
        ).message_post(
            body='<p>Hello from LINE</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor_partner.id,
        )

        # Verify: visitor_partner has no user_ids → classified as visitor
        is_visitor = self.session._is_visitor_message(msg)
        self.assertTrue(
            is_visitor,
            "LINE visitor messages must be classified as visitor messages for n8n"
        )
        mock_trigger.assert_called_once()

    # ------------------------------------------------------------------
    # 1.5 Visitor partner (no user_ids) triggers n8n, not LINE echo
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_visitor_partner_triggers_n8n_only(self, mock_trigger):
        """Visitor partner (no user_ids) should trigger n8n webhook."""
        msg = self.session.message_post(
            body='<p>Message from visitor partner</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor_partner.id,
        )

        self.assertTrue(
            self.session._is_visitor_message(msg),
            "Visitor partner without user_ids must be detected as visitor"
        )
        mock_trigger.assert_called_once()

    # ------------------------------------------------------------------
    # 1.6 OdooBot messages must NOT trigger n8n webhook
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_odoobot_does_not_trigger_n8n(self, mock_trigger):
        """OdooBot (base.partner_root) must NOT trigger n8n webhook."""
        odoobot = self.env.ref('base.partner_root', raise_if_not_found=False)
        if not odoobot:
            self.skipTest("OdooBot partner not found")

        msg = self.session.message_post(
            body='<p>System message from OdooBot</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=odoobot.id,
        )

        self.assertFalse(
            self.session._is_visitor_message(msg),
            "OdooBot must NOT be classified as visitor"
        )
        mock_trigger.assert_not_called()


# ===================================================================
# 2. CROSS-MODULE MESSAGE ROUTING
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestCrossModuleRouting(TransactionCase):
    """Test message routing decisions when both modules are active."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.livechat_channel = cls.env['im_livechat.channel'].create({
            'name': 'Routing Test Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/route-test',
        })

        cls.operator_user = cls.env['res.users'].create({
            'name': 'Route Operator',
            'login': 'route_op_test',
        })
        cls.operator = cls.operator_user.partner_id

        cls.session = _create_livechat_session(
            cls.env, cls.livechat_channel, 'route-test-uuid-001',
            name='Routing Test Session',
            operator_partner=cls.operator,
            line_user_id='U_route_test_user',
        )

        cls.visitor = cls.env['res.partner'].create({'name': 'Route Visitor'})

        cls.n8n_bot = cls.env.ref(
            'im_livechat_n8n.partner_n8n_bot', raise_if_not_found=False
        )

        cls.guest = cls.env['mail.guest'].create({'name': 'Route Guest'})

    # ------------------------------------------------------------------
    # 2.1 Operator message: NOT to n8n, YES to LINE
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_operator_message_routes_to_line_not_n8n(self, mock_n8n):
        """Operator message should go to LINE but NOT trigger n8n webhook."""
        msg = self.session.message_post(
            body='<p>Operator reply</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.operator.id,
        )

        # n8n: operator has user_ids → not visitor → no webhook
        self.assertFalse(self.session._is_visitor_message(msg))
        mock_n8n.assert_not_called()

        # LINE: author_guest_id=NULL, author_id=operator, type='comment'
        self.assertFalse(msg.author_guest_id)
        self.assertTrue(msg.author_id)
        self.assertEqual(msg.message_type, 'comment')

    # ------------------------------------------------------------------
    # 2.2 n8n bot response: NOT to n8n, YES to LINE
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_n8n_bot_response_routes_to_line_not_n8n(self, mock_n8n):
        """n8n bot reply should be sent to LINE but NOT re-trigger n8n."""
        msg = self.session.message_post(
            body='<p>AI says hello</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.n8n_bot.id,
        )

        # n8n: bot partner excluded → no webhook
        self.assertFalse(self.session._is_visitor_message(msg))
        mock_n8n.assert_not_called()

        # LINE: not guest, has author_id, type='comment' → passes LINE check
        self.assertFalse(msg.author_guest_id)
        self.assertTrue(msg.author_id)

    # ------------------------------------------------------------------
    # 2.3 System/notification messages: NOT to n8n, NOT to LINE
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_notification_message_routes_nowhere(self, mock_n8n):
        """Notification-type messages should not trigger either module."""
        msg = self.session.message_post(
            body='<p>System notification</p>',
            message_type='notification',
            subtype_xmlid='mail.mt_comment',
            author_id=self.operator.id,
        )

        mock_n8n.assert_not_called()
        self.assertNotEqual(msg.message_type, 'comment')

    # ------------------------------------------------------------------
    # 2.4 Non-livechat channel: neither module should fire
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_non_livechat_channel_no_routing(self, mock_n8n):
        """Regular channel messages should not trigger any integration."""
        regular = self.env['discuss.channel'].create({
            'name': 'Regular Channel',
            'channel_type': 'channel',
        })

        regular.message_post(
            body='<p>General chat</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )

        mock_n8n.assert_not_called()


# ===================================================================
# 3. AUTHOR TYPE DETECTION COMPATIBILITY
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestAuthorTypeDetection(TransactionCase):
    """Validate that both modules' author detection logic is compatible."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Author Detection Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/author-test',
        })

        cls.operator_user = cls.env['res.users'].create({
            'name': 'Auth Operator', 'login': 'auth_op_test'
        })
        cls.operator = cls.operator_user.partner_id

        cls.session = _create_livechat_session(
            cls.env, cls.channel, 'author-test-uuid',
            name='Author Detection Session',
            operator_partner=cls.operator,
        )

        cls.partner_visitor = cls.env['res.partner'].create({'name': 'Partner Visitor'})
        cls.n8n_bot = cls.env.ref('im_livechat_n8n.partner_n8n_bot', raise_if_not_found=False)
        cls.odoobot = cls.env.ref('base.partner_root', raise_if_not_found=False)
        cls.guest = cls.env['mail.guest'].create({'name': 'Auth Guest'})

    def _create_msg(self, author_id=None, author_guest_id=None, message_type='comment',
                     no_author=False):
        """Helper to create a mail.message for testing.

        Odoo 18 auto-fills author_id from env.user.partner_id on create.
        Pass no_author=True to explicitly set author_id=False for truly
        anonymous messages.
        """
        vals = {
            'body': '<p>Test</p>',
            'message_type': message_type,
            'model': 'discuss.channel',
            'res_id': self.session.id,
        }
        if no_author:
            vals['author_id'] = False
        elif author_id:
            vals['author_id'] = author_id
        if author_guest_id:
            vals['author_guest_id'] = author_guest_id
        return self.env['mail.message'].create(vals)

    def test_anonymous_visitor_detection(self):
        """author_id=NULL: n8n→visitor, LINE→skips (no author_id)."""
        msg = self._create_msg(no_author=True)  # explicitly no author
        self.assertTrue(self.session._is_visitor_message(msg))
        self.assertFalse(msg.author_id)

    def test_guest_author_detection(self):
        """author_guest_id=guest: LINE→skips (guest set)."""
        msg = self._create_msg(author_guest_id=self.guest.id)
        self.assertTrue(msg.author_guest_id)

    def test_partner_visitor_detection(self):
        """Partner without user_ids: n8n→visitor, LINE→would forward."""
        msg = self._create_msg(author_id=self.partner_visitor.id)
        self.assertTrue(self.session._is_visitor_message(msg))
        self.assertFalse(msg.author_guest_id)
        self.assertTrue(msg.author_id)

    def test_n8n_bot_detection(self):
        """n8n bot: n8n→excluded, LINE→would forward."""
        self.assertTrue(self.n8n_bot, "n8n bot must exist")
        msg = self._create_msg(author_id=self.n8n_bot.id)
        self.assertFalse(self.session._is_visitor_message(msg))
        self.assertFalse(msg.author_guest_id)
        self.assertTrue(msg.author_id)

    def test_operator_detection(self):
        """Operator: n8n→not visitor, LINE→would forward."""
        msg = self._create_msg(author_id=self.operator.id)
        self.assertFalse(self.session._is_visitor_message(msg))
        self.assertFalse(msg.author_guest_id)
        self.assertTrue(msg.author_id)

    def test_notification_message_type_detection(self):
        """message_type='notification': LINE→skips, n8n→depends on author."""
        msg = self._create_msg(
            author_id=self.partner_visitor.id,
            message_type='notification',
        )
        self.assertNotEqual(msg.message_type, 'comment')


# ===================================================================
# 4. WEBHOOK SECURITY BOUNDARY
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestWebhookSecurity(TransactionCase):
    """Validate API key, UUID format, and message size validation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Security Test Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/sec-test',
        })
        cls.api_key = cls.channel.n8n_api_key

        cls.session = _create_livechat_session(
            cls.env, cls.channel, 'sec-test-uuid-001',
            name='Security Test Session',
        )

    def test_api_key_length_and_format(self):
        """API key must be 43 chars (token_urlsafe(32))."""
        self.assertEqual(len(self.api_key), 43)
        self.assertTrue(
            re.match(r'^[A-Za-z0-9_-]+$', self.api_key),
            "API key must use URL-safe base64 characters"
        )

    def test_api_key_unique_per_channel(self):
        """Each channel should have a unique API key."""
        ch2 = self.env['im_livechat.channel'].create({
            'name': 'Security Channel 2',
            'n8n_enabled': True,
        })
        self.assertNotEqual(self.channel.n8n_api_key, ch2.n8n_api_key)

    def test_uuid_validation_valid_formats(self):
        """Valid Odoo 18 UUIDs should pass validation."""
        from odoo.addons.im_livechat_n8n.controllers.webhook import UUID_PATTERN

        valid_uuids = [
            'aYIEU268MM',
            'SQbykqRz7Z',
            'test-uuid-123-456',
            'ABCDEF',
            'a' * 50,
        ]
        for uuid in valid_uuids:
            self.assertTrue(UUID_PATTERN.match(uuid), f"UUID '{uuid}' should be valid")

    def test_uuid_validation_invalid_formats(self):
        """Invalid UUIDs should fail validation."""
        from odoo.addons.im_livechat_n8n.controllers.webhook import UUID_PATTERN

        invalid_uuids = [
            'short',
            'a' * 51,
            'invalid@uuid',
            'has spaces here',
            'sql;injection',
            '<script>alert</script>',
        ]
        for uuid in invalid_uuids:
            self.assertFalse(UUID_PATTERN.match(uuid), f"UUID '{uuid}' should be invalid")

    def test_message_size_validation(self):
        """Messages exceeding 10KB must be rejected."""
        from odoo.addons.im_livechat_n8n.controllers.webhook import N8NWebhookController

        ctrl = N8NWebhookController()
        self.assertTrue(ctrl._validate_message_size('Hello world'))
        self.assertTrue(ctrl._validate_message_size('x' * 10240))
        self.assertFalse(ctrl._validate_message_size('x' * 10241))
        self.assertTrue(ctrl._validate_message_size(''))
        self.assertTrue(ctrl._validate_message_size(None))

    def test_message_size_unicode(self):
        """Unicode characters should be measured in bytes, not chars."""
        from odoo.addons.im_livechat_n8n.controllers.webhook import N8NWebhookController

        ctrl = N8NWebhookController()
        # Chinese characters are 3 bytes each in UTF-8
        large_chinese = '中' * 3414  # 10242 bytes > 10240
        self.assertFalse(ctrl._validate_message_size(large_chinese))
        ok_chinese = '中' * 3413  # 10239 bytes ≤ 10240
        self.assertTrue(ctrl._validate_message_size(ok_chinese))


# ===================================================================
# 5. EDGE CASES & ERROR RESILIENCE
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestEdgeCases(TransactionCase):
    """Test unusual scenarios and error handling."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Edge Case Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/edge',
        })

        cls.session = _create_livechat_session(
            cls.env, cls.channel, 'edge-test-uuid-001',
            name='Edge Case Session',
            line_user_id='U_edge_test_user',
        )

        cls.visitor = cls.env['res.partner'].create({'name': 'Edge Visitor'})
        cls.n8n_bot = cls.env.ref('im_livechat_n8n.partner_n8n_bot', raise_if_not_found=False)

    # ------------------------------------------------------------------
    # 5.1 Webhook failure doesn't block message creation
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_webhook_failure_doesnt_block_message(self, mock_trigger):
        """If n8n webhook fails, the message should still be created."""
        mock_trigger.side_effect = Exception("Webhook connection refused")

        msg = self.session.message_post(
            body='<p>Message despite webhook failure</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )

        self.assertTrue(msg.exists())
        self.assertIn('Message despite webhook failure', msg.body)

    # ------------------------------------------------------------------
    # 5.2 Empty message body handling
    # ------------------------------------------------------------------
    def test_empty_body_message(self):
        """Messages with empty body should be handled gracefully."""
        msg = self.session.message_post(
            body='',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        self.assertTrue(msg.exists())

    # ------------------------------------------------------------------
    # 5.3 HTML in message body
    # ------------------------------------------------------------------
    def test_html_body_message(self):
        """HTML content should be handled without XSS issues."""
        html_body = '<p>Hello <b>world</b> &amp; <script>alert("xss")</script></p>'
        msg = self.session.message_post(
            body=html_body,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        self.assertTrue(msg.exists())

    # ------------------------------------------------------------------
    # 5.4 Rapid consecutive messages (burst)
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_rapid_consecutive_messages(self, mock_trigger):
        """Multiple rapid messages should each trigger webhook independently."""
        messages = []
        for i in range(5):
            msg = self.session.message_post(
                body=f'<p>Burst message {i}</p>',
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
                author_id=self.visitor.id,
            )
            messages.append(msg)

        self.assertEqual(len(messages), 5)
        for msg in messages:
            self.assertTrue(msg.exists())
        self.assertEqual(mock_trigger.call_count, 5)

    # ------------------------------------------------------------------
    # 5.5 Concurrent n8n and LINE on same session
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_both_modules_active_same_session(self, mock_n8n):
        """Both modules active on same session should not cause errors."""
        # Visitor message → n8n should fire
        msg_visitor = self.session.message_post(
            body='<p>Visitor says hi</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        self.assertTrue(msg_visitor.exists())
        self.assertEqual(mock_n8n.call_count, 1)

        # n8n bot reply → n8n should NOT fire
        mock_n8n.reset_mock()
        msg_bot = self.session.message_post(
            body='<p>Bot reply</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.n8n_bot.id,
        )
        self.assertTrue(msg_bot.exists())
        mock_n8n.assert_not_called()

    # ------------------------------------------------------------------
    # 5.6 Large payload webhook handling
    # ------------------------------------------------------------------
    def test_large_message_in_webhook_payload(self):
        """Webhook payload builder handles large messages correctly."""
        large_body = '<p>' + ('A' * 5000) + '</p>'
        msg = self.env['mail.message'].create({
            'body': large_body,
            'author_id': self.visitor.id,
            'model': 'discuss.channel',
            'res_id': self.session.id,
        })

        payload = self.channel._build_webhook_payload(
            'message_received', self.session, msg
        )
        self.assertIn('A' * 100, payload['message']['body'])

    # ------------------------------------------------------------------
    # 5.7 Special characters in message
    # ------------------------------------------------------------------
    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_special_characters_in_message(self, mock_trigger):
        """Messages with emoji, CJK, RTL text should work."""
        special_messages = [
            '<p>你好世界 🌍</p>',
            '<p>مرحبا بالعالم</p>',
            '<p>こんにちは世界</p>',
            '<p>Line1\nLine2\n\nLine4</p>',
            '<p>&lt;tag&gt; &amp; "quotes"</p>',
        ]

        for body in special_messages:
            msg = self.session.message_post(
                body=body,
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
                author_id=self.visitor.id,
            )
            self.assertTrue(msg.exists(), f"Failed for body: {body}")


# ===================================================================
# 6. CONFIGURATION COMBINATIONS
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestConfigCombinations(TransactionCase):
    """Test all combinations of n8n/LINE enabled/disabled."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.visitor = cls.env['res.partner'].create({'name': 'Config Visitor'})
        cls.n8n_bot = cls.env.ref('im_livechat_n8n.partner_n8n_bot', raise_if_not_found=False)

    def _make_channel_and_session(self, n8n_enabled=True, line_user_id=None):
        """Helper to create livechat channel + session."""
        channel = self.env['im_livechat.channel'].create({
            'name': f'Config Ch n8n={n8n_enabled} line={bool(line_user_id)}',
            'n8n_enabled': n8n_enabled,
            'n8n_webhook_url': 'http://n8n:5678/webhook/cfg' if n8n_enabled else False,
        })
        session = _create_livechat_session(
            self.env, channel, f'cfg-{channel.id}-uuid',
            name='Config Session',
            line_user_id=line_user_id,
        )
        return channel, session

    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_n8n_on_line_off(self, mock_n8n):
        """n8n enabled, no LINE → only n8n webhook fires for visitor msgs."""
        _, session = self._make_channel_and_session(n8n_enabled=True, line_user_id=None)
        session.message_post(
            body='<p>Hello</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        mock_n8n.assert_called_once()

    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_n8n_off_line_on(self, mock_n8n):
        """n8n disabled, LINE present → n8n should not fire."""
        _, session = self._make_channel_and_session(n8n_enabled=False, line_user_id='U_lineonly')
        session.message_post(
            body='<p>Hello</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        mock_n8n.assert_not_called()

    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_both_on(self, mock_n8n):
        """Both enabled → n8n fires for visitor, not for bot."""
        _, session = self._make_channel_and_session(n8n_enabled=True, line_user_id='U_both')

        session.message_post(
            body='<p>Visitor</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        self.assertEqual(mock_n8n.call_count, 1)

        mock_n8n.reset_mock()
        session.message_post(
            body='<p>Bot</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.n8n_bot.id,
        )
        mock_n8n.assert_not_called()

    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_both_off(self, mock_n8n):
        """Both disabled → no integrations fire."""
        _, session = self._make_channel_and_session(n8n_enabled=False, line_user_id=None)
        session.message_post(
            body='<p>Quiet</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        mock_n8n.assert_not_called()

    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_toggle_n8n_mid_session(self, mock_n8n):
        """Disabling n8n mid-session should stop webhook triggers."""
        channel, session = self._make_channel_and_session(n8n_enabled=True)

        session.message_post(
            body='<p>Before disable</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        self.assertEqual(mock_n8n.call_count, 1)

        channel.write({'n8n_enabled': False})
        mock_n8n.reset_mock()

        session.message_post(
            body='<p>After disable</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor.id,
        )
        mock_n8n.assert_not_called()

    def test_webhook_url_change_mid_session(self):
        """Changing webhook URL mid-session should use the new URL."""
        channel, _ = self._make_channel_and_session(n8n_enabled=True)
        channel.write({'n8n_webhook_url': 'http://n8n:5678/webhook/new-endpoint'})
        self.assertEqual(channel.n8n_webhook_url, 'http://n8n:5678/webhook/new-endpoint')

    def test_api_key_regeneration_invalidates_old(self):
        """After regeneration, old API key should no longer match."""
        channel, _ = self._make_channel_and_session(n8n_enabled=True)
        old_key = channel.n8n_api_key
        channel.action_regenerate_api_key()
        new_key = channel.n8n_api_key

        self.assertNotEqual(old_key, new_key)
        found = self.env['im_livechat.channel'].sudo().search(
            [('n8n_api_key', '=', old_key)], limit=1
        )
        self.assertFalse(found)


# ===================================================================
# 7. WEBHOOK PAYLOAD STRUCTURE
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestWebhookPayloadStructure(TransactionCase):
    """Validate webhook payload format for n8n workflow compatibility."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Payload Test Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/payload-test',
        })
        cls.session = _create_livechat_session(
            cls.env, cls.channel, 'payload-test-uuid',
            name='Payload Test Session',
        )
        cls.visitor = cls.env['res.partner'].create({'name': 'Payload Visitor'})

    def test_payload_event_type_field(self):
        """Payload must include event_type for n8n workflow routing."""
        msg = self.env['mail.message'].create({
            'body': '<p>Test</p>',
            'author_id': self.visitor.id,
            'model': 'discuss.channel',
            'res_id': self.session.id,
        })
        payload = self.channel._build_webhook_payload('message_received', self.session, msg)
        self.assertEqual(payload['event_type'], 'message_received')

    def test_payload_session_uuid(self):
        """Payload must include session UUID for callback routing."""
        payload = self.channel._build_webhook_payload('message_received', self.session)
        self.assertEqual(payload['session']['uuid'], 'payload-test-uuid')

    def test_payload_message_body_html(self):
        """Payload message body preserves HTML for n8n processing."""
        msg = self.env['mail.message'].create({
            'body': '<p>Hello <b>world</b></p>',
            'author_id': self.visitor.id,
            'model': 'discuss.channel',
            'res_id': self.session.id,
        })
        payload = self.channel._build_webhook_payload('message_received', self.session, msg)
        self.assertIn('<b>world</b>', payload['message']['body'])

    def test_payload_visitor_author_type(self):
        """Visitor messages should have author_type='visitor'."""
        msg = self.env['mail.message'].create({
            'body': '<p>Test</p>',
            'author_id': self.visitor.id,
            'model': 'discuss.channel',
            'res_id': self.session.id,
        })
        payload = self.channel._build_webhook_payload('message_received', self.session, msg)
        self.assertEqual(payload['message']['author_type'], 'visitor')

    def test_payload_operator_author_type(self):
        """Operator messages should have author_type='operator'."""
        op_user = self.env['res.users'].create({
            'name': 'Payload Op', 'login': 'payload_op_test'
        })
        msg = self.env['mail.message'].create({
            'body': '<p>Test</p>',
            'author_id': op_user.partner_id.id,
            'model': 'discuss.channel',
            'res_id': self.session.id,
        })
        payload = self.channel._build_webhook_payload('message_received', self.session, msg)
        self.assertEqual(payload['message']['author_type'], 'operator')

    def test_payload_metadata_callback_url(self):
        """Payload metadata must include callback URL and API key header."""
        payload = self.channel._build_webhook_payload('message_received', self.session)
        self.assertIn('/im_livechat_n8n/webhook', payload['metadata']['callback_url'])
        self.assertEqual(payload['metadata']['api_key_header'], 'X-API-Key')

    def test_payload_channel_info(self):
        """Payload must include channel id and name."""
        payload = self.channel._build_webhook_payload('message_received', self.session)
        self.assertEqual(payload['channel']['id'], self.channel.id)
        self.assertEqual(payload['channel']['name'], 'Payload Test Channel')

    def test_payload_timestamp_format(self):
        """Timestamp must be ISO 8601 with Z suffix."""
        payload = self.channel._build_webhook_payload('message_received', self.session)
        ts = payload['timestamp']
        self.assertTrue(ts.endswith('Z'), f"Timestamp must end with Z: {ts}")
        from datetime import datetime
        datetime.fromisoformat(ts.rstrip('Z'))


# ===================================================================
# 8. INBOUND WEBHOOK CONTROLLER TESTS (HTTP-level)
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestInboundWebhookHTTP(HttpCase):
    """HTTP-level tests for the /im_livechat_n8n/webhook endpoint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'HTTP Test Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/http-test',
        })
        cls.api_key = cls.channel.n8n_api_key

        cls.session = _create_livechat_session(
            cls.env, cls.channel, 'http-test-uuid-001',
            name='HTTP Test Session',
        )

    def _post_webhook(self, payload, api_key=None, content_type='application/json'):
        """Helper to POST to the webhook endpoint."""
        headers = {'Content-Type': content_type}
        if api_key:
            headers['X-API-Key'] = api_key
        return self.url_open(
            '/im_livechat_n8n/webhook',
            data=json.dumps(payload),
            headers=headers,
        )

    def test_valid_request_returns_200(self):
        """Valid request with correct API key and session should return 200."""
        resp = self._post_webhook(
            payload={
                'action': 'send_message',
                'session_uuid': 'http-test-uuid-001',
                'message': {'body': 'Hello from test', 'author_name': 'Test Bot'},
            },
            api_key=self.api_key,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['status'], 'ok')

    def test_missing_api_key_returns_401(self):
        """Request without API key should return 401."""
        resp = self._post_webhook(
            payload={'session_uuid': 'http-test-uuid-001', 'message': {'body': 'No key'}},
        )
        self.assertEqual(resp.status_code, 401)

    def test_invalid_api_key_returns_401(self):
        """Request with wrong API key should return 401."""
        resp = self._post_webhook(
            payload={'session_uuid': 'http-test-uuid-001', 'message': {'body': 'Wrong key'}},
            api_key='totally-wrong-key-value',
        )
        self.assertEqual(resp.status_code, 401)

    def test_missing_session_uuid_returns_400(self):
        """Request without session_uuid should return 400."""
        resp = self._post_webhook(
            payload={'message': {'body': 'No UUID'}},
            api_key=self.api_key,
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_message_body_returns_400(self):
        """Request without message body should return 400."""
        resp = self._post_webhook(
            payload={'session_uuid': 'http-test-uuid-001', 'message': {}},
            api_key=self.api_key,
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_uuid_format_returns_400(self):
        """Request with malformed UUID should return 400."""
        resp = self._post_webhook(
            payload={'session_uuid': 'invalid@@@uuid!!!', 'message': {'body': 'Bad UUID'}},
            api_key=self.api_key,
        )
        self.assertEqual(resp.status_code, 400)

    def test_nonexistent_session_returns_404(self):
        """Request with valid format but non-existent UUID should return 404."""
        resp = self._post_webhook(
            payload={'session_uuid': 'NONEXISTENT-UUID-999', 'message': {'body': 'Ghost session'}},
            api_key=self.api_key,
        )
        self.assertEqual(resp.status_code, 404)

    def test_oversized_message_returns_400(self):
        """Request with message body > 10KB should return 400."""
        resp = self._post_webhook(
            payload={'session_uuid': 'http-test-uuid-001', 'message': {'body': 'X' * 11000}},
            api_key=self.api_key,
        )
        self.assertEqual(resp.status_code, 400)

    def test_message_posted_to_correct_session(self):
        """Valid webhook should create message in the correct session."""
        msg_count_before = self.env['mail.message'].search_count([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
        ])

        self._post_webhook(
            payload={
                'action': 'send_message',
                'session_uuid': 'http-test-uuid-001',
                'message': {'body': 'Integration test message'},
            },
            api_key=self.api_key,
        )

        msg_count_after = self.env['mail.message'].search_count([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
        ])
        self.assertEqual(msg_count_after, msg_count_before + 1)

    def test_webhook_log_created_on_success(self):
        """Successful webhook should create an inbound log entry."""
        log_count_before = self.env['n8n.webhook.log'].search_count([
            ('event_type', '=', 'inbound'),
        ])

        self._post_webhook(
            payload={'session_uuid': 'http-test-uuid-001', 'message': {'body': 'Log test'}},
            api_key=self.api_key,
        )

        log_count_after = self.env['n8n.webhook.log'].search_count([
            ('event_type', '=', 'inbound'),
        ])
        self.assertGreater(log_count_after, log_count_before)

    def test_webhook_log_created_on_auth_failure(self):
        """Failed auth should also create a log entry."""
        log_count_before = self.env['n8n.webhook.log'].search_count([
            ('event_type', '=', 'inbound'), ('status', '=', 'failed'),
        ])

        self._post_webhook(
            payload={'session_uuid': 'http-test-uuid-001', 'message': {'body': 'Bad auth'}},
            api_key='wrong-key',
        )

        log_count_after = self.env['n8n.webhook.log'].search_count([
            ('event_type', '=', 'inbound'), ('status', '=', 'failed'),
        ])
        self.assertGreater(log_count_after, log_count_before)


# ===================================================================
# 9. FULL INTEGRATION SCENARIO (MOCK-BASED)
# ===================================================================
@tagged('post_install', '-at_install', 'n8n_line_integration')
class TestFullIntegrationScenario(TransactionCase):
    """Simulate the complete LINE → n8n → LLM → Odoo → LINE flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Full Integration Channel',
            'n8n_enabled': True,
            'n8n_webhook_url': 'http://n8n:5678/webhook/full-test',
        })

        cls.session = _create_livechat_session(
            cls.env, cls.channel, 'full-integ-uuid-001',
            name='Full Integration Session',
            line_user_id='U_full_integ_user',
        )

        cls.guest = cls.env['mail.guest'].create({'name': 'LINE User'})
        cls.visitor_partner = cls.env['res.partner'].create({
            'name': 'LINE Visitor User',
        })
        cls.n8n_bot = cls.env.ref('im_livechat_n8n.partner_n8n_bot', raise_if_not_found=False)

    @patch('odoo.addons.im_livechat_n8n.models.im_livechat_channel.ImLivechatChannel._trigger_n8n_webhook')
    def test_full_scenario_line_to_n8n_to_line(self, mock_webhook):
        """Simulate: LINE msg → n8n webhook → bot reply → LINE forward.

        Step 1: LINE webhook posts visitor message (with from_line_webhook context)
        Step 2: n8n webhook fires for visitor message
        Step 3: n8n callback posts bot reply
        Step 4: Bot reply passes LINE forwarding check
        Step 5: No infinite loop
        """
        # ---- Step 1: LINE visitor message arrives ----
        # Real LINE flow: visitor messages have author_id set to a visitor
        # partner (no user_ids), which _is_visitor_message detects correctly.
        visitor_msg = self.session.with_context(
            from_line_webhook=True,
        ).message_post(
            body='<p>你好，我想問一下</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.visitor_partner.id,
        )
        self.assertTrue(visitor_msg.exists(), "Visitor message must be created")

        # ---- Step 2: n8n webhook should have been triggered ----
        mock_webhook.assert_called_once()
        call_args = mock_webhook.call_args
        # _trigger_n8n_webhook is called with keyword args
        event_type = call_args[1].get('event_type') or (call_args[0][0] if call_args[0] else None)
        self.assertEqual(event_type, 'message_received')

        # ---- Step 3: n8n callback posts bot reply ----
        mock_webhook.reset_mock()
        bot_reply = self.session.message_post(
            body='<p>您好！我是AI助手，很高興為您服務。</p>',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.n8n_bot.id,
        )
        self.assertTrue(bot_reply.exists(), "Bot reply must be created")

        # ---- Step 4: Bot reply should NOT re-trigger n8n ----
        mock_webhook.assert_not_called()

        # ---- Step 5: Bot reply should pass LINE forwarding check ----
        self.assertFalse(bot_reply.author_guest_id, "Bot has no guest_id")
        self.assertTrue(bot_reply.author_id, "Bot has author_id")
        self.assertEqual(bot_reply.message_type, 'comment')

        # ---- Verify message count (no infinite loop) ----
        recent_msgs = self.env['mail.message'].search([
            ('res_id', '=', self.session.id),
            ('model', '=', 'discuss.channel'),
        ], order='id desc', limit=5)
        msg_bodies = [m.body for m in recent_msgs[:2]]
        self.assertIn('AI助手', msg_bodies[0])
        self.assertIn('我想問一下', msg_bodies[1])
