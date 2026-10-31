# -*- coding: utf-8 -*-

import html as html_lib
import logging
import re
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)

# Regex to strip HTML tags for building clean LLM context
_HTML_TAG_RE = re.compile(r'<[^>]+>')

# Prefix of a human operator's message in the LLM history (P1-3).
OPERATOR_PREFIX = '[真人客服 %s] '
# Added to the system prompt when the history contains operator messages.
OPERATOR_SYSTEM_NOTE = (
    '對話中以「[真人客服 姓名]」開頭的 assistant 訊息是真人客服同事的回覆，'
    '不是你說的；不要推翻、否認或更改同事的說法與承諾，必要時請訪客等候同事處理。'
)

# Author kinds returned by _ai_author_kind
AUTHOR_VISITOR = 'visitor'
AUTHOR_OPERATOR = 'operator'  # internal (non-share) user: a human operator
AUTHOR_BOT = 'bot'  # AI bot, OdooBot, or a session operator without internal user (chatbot)


def _clean_text(body):
    """Message HTML -> plain text."""
    return html_lib.unescape(_HTML_TAG_RE.sub('', body or '')).strip()


class DiscussChannel(models.Model):
    """
    Extend discuss.channel to intercept livechat messages and trigger AI responses.

    This extension hooks into the message notification pipeline to call
    OpenAI-compatible LLM APIs when visitors send messages in livechat sessions.
    """
    _inherit = 'discuss.channel'

    ai_paused_until = fields.Datetime(
        string='AI Paused Until',
        readonly=True,
        copy=False,
        help='The AI does not answer this live chat conversation before this '
             'time: an operator replied or the visitor asked for a human.',
    )

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """
        Override _notify_thread to trigger AI responses for visitor messages.

        Called after each message is posted to the channel, inside the
        poster's transaction:
        - a human operator's message pauses the AI for the session (P1-2);
        - a visitor message asking for a human queues a handoff job (fixed
          notice + operator notification) and pauses the AI;
        - any other visitor message in an AI-enabled, non-paused session
          queues an im_livechat.ai.job in that same transaction (so the job
          exists exactly when the message is committed); processing starts
          after commit.
        """
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)

        try:
            # Savepoint: a failure here must not abort the poster's message.
            # sudo: the poster may be a guest, a portal or a public user.
            with self.env.cr.savepoint():
                self.sudo()._ai_handle_new_message(message.sudo())
        except Exception as e:
            _logger.warning(
                "Failed to trigger AI response for channel %s: %s",
                self.id, e, exc_info=True,
            )

        return rdata

    def _ai_handle_new_message(self, message):
        """Pause, hand off or queue an AI reply for a newly posted message."""
        if len(self) != 1 or not self._ai_session_enabled():
            return
        kind = self._ai_author_kind(message)
        if kind == AUTHOR_OPERATOR:
            if message.message_type == 'comment':
                self._ai_pause()
            return
        if kind != AUTHOR_VISITOR or self._ai_is_paused():
            return
        channel = self.livechat_channel_id
        if channel._ai_is_handoff_request(_clean_text(message.body)):
            self._ai_pause()
            channel._trigger_ai_response(discuss_channel=self, message=message, kind='handoff')
        else:
            channel._trigger_ai_response(discuss_channel=self, message=message)

    def _ai_session_enabled(self):
        self.ensure_one()
        return bool(
            self.channel_type == 'livechat'
            and self.livechat_channel_id
            and self.livechat_channel_id.ai_enabled
        )

    def _should_trigger_ai_response(self, message):
        """
        Determine if an AI response should be triggered for this message.

        Checks:
        1. Channel type is livechat
        2. Channel has associated livechat_channel_id
        3. AI integration is enabled for the channel
        4. Message is from a visitor (not operator, bot, or system)
        5. The AI is not paused for this session (human handoff)
        """
        if not self._ai_session_enabled():
            return False

        if not self._is_visitor_message(message):
            return False

        return not self._ai_is_paused()

    # --- Pause (human handoff, P1-2) ---

    def _ai_is_paused(self):
        """True while ai_paused_until is in the future (reads the committed
        value, so a running job sees an operator's post made meanwhile)."""
        self.ensure_one()
        self.invalidate_recordset(['ai_paused_until'])
        paused_until = self.sudo().ai_paused_until
        return bool(paused_until and paused_until > fields.Datetime.now())

    def _ai_pause(self, minutes=None):
        """Pause the AI for this session (extends, never shortens, a pause)."""
        self.ensure_one()
        if minutes is None:
            minutes = self.livechat_channel_id._ai_pause_minutes()
        until = fields.Datetime.now() + timedelta(minutes=minutes)
        current = self.sudo().ai_paused_until
        if not current or current < until:
            self.sudo().write({'ai_paused_until': until})
        return until

    # --- Who wrote a message (P1-1) ---

    def _ai_author_kind(self, message):
        """
        Classify the author of a message: visitor, operator or bot.

        Bot exclusion checks come FIRST to prevent response loops:
        - the channel's AI bot partner and the default AI bot partner
        - OdooBot (base.partner_root)
        Then:
        - guest (author_guest_id, e.g. LINE user) or no author: visitor
        - author with an internal (non-share) user: human operator
        - the session's assigned operator (livechat_operator_id) without an
          internal user, e.g. an Odoo chatbot: bot
        - anyone else, including portal users and the public user: visitor
        """
        author = message.author_id
        if author:
            bot_ids = self.env['im_livechat.channel']._ai_bot_partner_ids(self.livechat_channel_id)
            if author.id in bot_ids:
                return AUTHOR_BOT
            odoobot = self.env.ref('base.partner_root', raise_if_not_found=False)
            if odoobot and author.id == odoobot.id:
                return AUTHOR_BOT

        if message.author_guest_id or not author:
            return AUTHOR_VISITOR

        users = author.sudo().with_context(active_test=False).user_ids
        if any(not user.share for user in users):
            return AUTHOR_OPERATOR
        if author == self.livechat_operator_id:
            return AUTHOR_BOT
        return AUTHOR_VISITOR

    def _is_visitor_message(self, message):
        """
        Check if a message is from a visitor (not operator, bot, or system).

        A visitor is a guest, an anonymous author, or a partner without any
        internal user account: portal users are visitors (P1-1). The AI bot
        partners, OdooBot, the session's assigned operator and internal users
        are never visitors.
        """
        return self._ai_author_kind(message) == AUTHOR_VISITOR

    def _build_llm_messages(self, livechat_channel):
        """
        Build the messages array for an OpenAI-compatible chat completions API.

        Fetches the most recent N messages from this discuss.channel,
        determines the role for each (visitor: user; AI bot and human
        operators: assistant, operators marked "[真人客服 name]") and
        prepends the system message: the channel prompt, a note about
        operator messages when there are any, and the fixed product rules.

        Args:
            livechat_channel (im_livechat.channel): The livechat channel config

        Returns:
            list: List of dicts with 'role' and 'content' keys
        """
        max_history = max(1, min(livechat_channel.ai_max_history or 50, 200))

        # Fetch recent messages ordered by id (chronological)
        recent_messages = self.env['mail.message'].search(
            [
                ('res_id', '=', self.id),
                ('model', '=', 'discuss.channel'),
                ('message_type', 'in', ['comment', 'email']),
            ],
            order='id desc',
            limit=max_history,
        )

        # Reverse to chronological order
        recent_messages = recent_messages.sorted('id')

        # Roles (P1-3): visitor -> user; AI bot -> assistant; human operator
        # -> assistant marked "[真人客服 name]" so the model treats it as a
        # colleague's reply instead of a visitor request.
        history = []
        has_operator = False
        for msg in recent_messages:
            clean_body = _clean_text(msg.body)
            if not clean_body:
                continue
            kind = self._ai_author_kind(msg)
            if kind == AUTHOR_VISITOR:
                role = 'user'
            elif kind == AUTHOR_OPERATOR:
                role = 'assistant'
                has_operator = True
                name = msg.author_id.sudo().name or ''
                clean_body = (OPERATOR_PREFIX % name) + clean_body
            else:
                role = 'assistant'
            history.append({'role': role, 'content': clean_body})

        # The product rules (package E, P1-8) are always appended: prices,
        # specifications and stock only come from the product tools.
        parts = [livechat_channel.ai_system_prompt or '']
        if has_operator:
            parts.append(OPERATOR_SYSTEM_NOTE)
        parts.append(livechat_channel._ai_product_guard())
        system_prompt = '\n\n'.join(p.strip() for p in parts if p and p.strip())
        return [{'role': 'system', 'content': system_prompt}] + history
