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
# Added to the system prompt of a reply sent when a handoff wait ended
# without an operator (auto-resume, P0-1).
RESUME_SYSTEM_NOTE = (
    '訪客先前要求真人客服，但客服人員在等候時間內沒有回覆，現在由你繼續服務。'
    '對話中「已幫您通知客服」「已通知客服人員」這類訊息是系統自動送出的等候通知，不是回答；'
    '請一併回答訪客在等候期間提出、還沒有被回答的所有問題。'
)

# Why the AI of a session is paused (discuss.channel.ai_pause_reason)
PAUSE_OPERATOR = 'operator'
PAUSE_HANDOFF = 'handoff'

# Author kinds returned by _ai_author_kind
AUTHOR_VISITOR = 'visitor'
AUTHOR_OPERATOR = 'operator'  # internal (non-share) user who is not the visitor: a human operator
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
    ai_pause_reason = fields.Selection(
        selection=[
            (PAUSE_OPERATOR, 'Operator replied'),
            (PAUSE_HANDOFF, 'Visitor asked for a human'),
        ],
        string='AI Pause Reason',
        readonly=True,
        copy=False,
        help='Operator replied: the AI stays silent until the pause ends. Visitor '
             'asked for a human: the AI waits for an operator; if none replies, it '
             'answers the messages written meanwhile when the wait ends.',
    )
    ai_pause_notice_sent = fields.Boolean(
        string='AI Waiting Notice Sent',
        readonly=True,
        copy=False,
        help='The waiting notice was already sent during the current handoff wait.',
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
                # Operator pause; also ends a handoff wait (the operator took over).
                self._ai_pause(reason=PAUSE_OPERATOR)
            return
        # Only what the visitor writes is answered: not the rating/feedback
        # notification posted as the visitor after the chat, nor other
        # notifications.
        if message.message_type != 'comment':
            return
        if kind != AUTHOR_VISITOR:
            return
        channel = self.livechat_channel_id
        if self._ai_is_paused():
            if self.ai_pause_reason == PAUSE_HANDOFF:
                self._ai_handle_waiting_visitor(message)
            # Operator pause: no reply, no notice.
            return
        if channel._ai_is_handoff_request(_clean_text(message.body)):
            self._ai_pause(minutes=channel._ai_wait_minutes(), reason=PAUSE_HANDOFF)
            channel._trigger_ai_response(discuss_channel=self, message=message, kind='handoff')
        else:
            channel._trigger_ai_response(discuss_channel=self, message=message)

    def _ai_handle_waiting_visitor(self, message):
        """Visitor message during a handoff wait: the first one gets the
        waiting notice (once per wait), every one gets a deferred reply job
        due when the wait ends (the newest answers them all; an operator's
        reply cancels them)."""
        channel = self.livechat_channel_id
        if not self.ai_pause_notice_sent:
            # Written in the visitor's transaction: two concurrent visitor
            # posts serialize on this row, so only one notice is queued.
            self.write({'ai_pause_notice_sent': True})
            channel._trigger_ai_response(discuss_channel=self, message=message, kind='notice')
        channel._trigger_ai_response(discuss_channel=self, message=message, run_after=self.ai_paused_until)

    def _ai_session_enabled(self):
        self.ensure_one()
        # sudo: the ai_* fields are restricted to Live Chat managers.
        livechat_channel = self.sudo().livechat_channel_id
        return bool(
            self.channel_type == 'livechat'
            and livechat_channel
            and livechat_channel.ai_enabled
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

    def _ai_pause(self, minutes=None, reason=PAUSE_OPERATOR):
        """Pause the AI for this session (extends, never shortens, a pause).

        reason 'operator': an operator replied (ai_handoff_pause_minutes by
        default); it also turns a handoff wait into an operator pause.
        reason 'handoff': the visitor asked for a human (called when the
        session is not paused); a new wait, its waiting notice not sent yet.
        """
        self.ensure_one()
        session = self.sudo()
        if minutes is None:
            minutes = session.livechat_channel_id._ai_pause_minutes()
        until = fields.Datetime.now() + timedelta(minutes=minutes)
        current = session.ai_paused_until
        vals = {}
        if not current or current < until:
            vals['ai_paused_until'] = until
        if reason == PAUSE_HANDOFF:
            vals.update(ai_pause_reason=PAUSE_HANDOFF, ai_pause_notice_sent=False)
        elif session.ai_pause_reason != PAUSE_OPERATOR:
            vals['ai_pause_reason'] = PAUSE_OPERATOR
        if vals:
            session.write(vals)
        return session.ai_paused_until

    def _ai_waiting_for_human(self):
        """True during a handoff wait that no operator answered yet."""
        self.ensure_one()
        return self._ai_is_paused() and self.sudo().ai_pause_reason == PAUSE_HANDOFF

    # --- Who wrote a message (P1-1) ---

    def _ai_bot_ids(self):
        """Partner ids that are bots: the AI bots and OdooBot."""
        ids = set(self.env['im_livechat.channel']._ai_bot_partner_ids(self.sudo().livechat_channel_id))
        odoobot = self.env.ref('base.partner_root', raise_if_not_found=False)
        if odoobot:
            ids.add(odoobot.id)
        return ids

    def _ai_visitor_partner(self):
        """Partner of the session's visitor member, or an empty recordset.

        Odoo creates the operator's member first and then, in the same
        transaction, the visitor's (a logged-in visitor's partner, or a guest
        member without partner; the LINE connector does the same); operators
        invited later get later members, created in a later transaction. So
        the visitor member is the first member created with the session that
        is neither the session operator nor a bot.
        """
        self.ensure_one()
        session = self.sudo()
        operator = session.livechat_operator_id
        bot_ids = self._ai_bot_ids()
        for member in session.channel_member_ids.sorted('id'):
            if member.create_date and session.create_date and member.create_date > session.create_date:
                break  # joined later: an invited operator, never the visitor
            partner = member.partner_id
            if partner and (partner == operator or partner.id in bot_ids):
                continue
            return partner
        return self.env['res.partner']

    def _ai_author_kind(self, message, visitor_partner=None):
        """
        Classify the author of a message: visitor, operator or bot.

        Bot exclusion checks come FIRST to prevent response loops:
        - the channel's AI bot partner and the default AI bot partner
        - OdooBot (base.partner_root)
        Then:
        - guest (author_guest_id, e.g. LINE user) or no author: visitor
        - author with an internal (non-share) user: human operator, except
          when the author is the session's visitor member (an employee
          chatting on the website, e.g. the internal trial) and not the
          session operator: visitor
        - the session's assigned operator (livechat_operator_id) without an
          internal user, e.g. an Odoo chatbot: bot
        - anyone else, including portal users and the public user: visitor

        Args:
            visitor_partner (res.partner|None): result of
                _ai_visitor_partner(), when the caller classifies many messages
        """
        author = message.author_id
        if author and author.id in self._ai_bot_ids():
            return AUTHOR_BOT

        if message.author_guest_id or not author:
            return AUTHOR_VISITOR

        users = author.sudo().with_context(active_test=False).user_ids
        if any(not user.share for user in users):
            if author == self.sudo().livechat_operator_id:
                return AUTHOR_OPERATOR
            if visitor_partner is None:
                visitor_partner = self._ai_visitor_partner()
            if visitor_partner and author == visitor_partner:
                return AUTHOR_VISITOR
            return AUTHOR_OPERATOR
        if author == self.sudo().livechat_operator_id:
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

    def _ai_context_messages(self, livechat_channel):
        """The most recent ai_max_history comment/email messages of this
        session, oldest first (what the LLM gets as conversation)."""
        max_history = livechat_channel._ai_max_history_value()
        recent_messages = self.env['mail.message'].search(
            [
                ('res_id', '=', self.id),
                ('model', '=', 'discuss.channel'),
                ('message_type', 'in', ['comment', 'email']),
            ],
            order='id desc',
            limit=max_history,
        )
        return recent_messages.sorted('id')

    @staticmethod
    def _ai_operator_name(author):
        """Name visitors see for an operator: the Live Chat username when set."""
        author = author.sudo()
        user = author.with_context(active_test=False).user_ids.filtered(lambda u: not u.share)[:1]
        return (user.livechat_username if user else '') or author.name or ''

    def _build_llm_messages(self, livechat_channel, recent_messages=None, resume=False):
        """
        Build the messages array for an OpenAI-compatible chat completions API.

        Fetches the most recent N messages from this discuss.channel,
        determines the role for each (visitor: user; AI bot and human
        operators: assistant, operators marked "[真人客服 name]" with the
        name visitors see) and prepends the system message: the fixed
        identity block (P1-8), the channel prompt, a note about operator
        messages when there are any, the resume note for a reply sent after
        an unanswered handoff wait, and the fixed product rules.

        Args:
            livechat_channel (im_livechat.channel): The livechat channel config
            recent_messages (mail.message|None): result of
                _ai_context_messages() when the caller already fetched it
            resume (bool): the reply ends a handoff wait no operator answered

        Returns:
            list: List of dicts with 'role' and 'content' keys
        """
        if recent_messages is None:
            recent_messages = self._ai_context_messages(livechat_channel)
        visitor_partner = self._ai_visitor_partner()

        # Roles (P1-3): visitor -> user; AI bot -> assistant; human operator
        # -> assistant marked "[真人客服 name]" so the model treats it as a
        # colleague's reply instead of a visitor request.
        history = []
        has_operator = False
        for msg in recent_messages:
            clean_body = _clean_text(msg.body)
            if not clean_body:
                continue
            kind = self._ai_author_kind(msg, visitor_partner=visitor_partner)
            if kind == AUTHOR_VISITOR:
                role = 'user'
            elif kind == AUTHOR_OPERATOR:
                role = 'assistant'
                has_operator = True
                clean_body = (OPERATOR_PREFIX % self._ai_operator_name(msg.author_id)) + clean_body
            else:
                role = 'assistant'
            history.append({'role': role, 'content': clean_body})

        # The product rules (package E, P1-8) are always appended: prices,
        # specifications and stock only come from the product tools.
        parts = [livechat_channel._ai_identity_block(), livechat_channel.ai_system_prompt or '']
        if has_operator:
            parts.append(OPERATOR_SYSTEM_NOTE)
        if resume:
            parts.append(RESUME_SYSTEM_NOTE)
        parts.append(livechat_channel._ai_product_guard())
        system_prompt = '\n\n'.join(p.strip() for p in parts if p and p.strip())
        return [{'role': 'system', 'content': system_prompt}] + history
