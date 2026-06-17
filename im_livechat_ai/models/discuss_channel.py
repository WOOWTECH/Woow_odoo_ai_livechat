# -*- coding: utf-8 -*-

import html as html_lib
import logging
import re

from odoo import models

_logger = logging.getLogger(__name__)

# Regex to strip HTML tags for building clean LLM context
_HTML_TAG_RE = re.compile(r'<[^>]+>')


class DiscussChannel(models.Model):
    """
    Extend discuss.channel to intercept livechat messages and trigger AI responses.

    This extension hooks into the message notification pipeline to call
    OpenAI-compatible LLM APIs when visitors send messages in livechat sessions.
    """
    _inherit = 'discuss.channel'

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """
        Override _notify_thread to trigger AI responses for visitor messages.

        Called after each message is posted to the channel. We hook into it
        to trigger LLM API calls for visitor messages in AI-enabled channels.
        """
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)

        try:
            if self._should_trigger_ai_response(message):
                self.livechat_channel_id._trigger_ai_response(
                    discuss_channel=self,
                    message=message,
                )
        except Exception as e:
            _logger.warning(
                "Failed to trigger AI response for channel %s: %s",
                self.id, e, exc_info=True,
            )

        return rdata

    def _should_trigger_ai_response(self, message):
        """
        Determine if an AI response should be triggered for this message.

        Checks:
        1. Channel type is livechat
        2. Channel has associated livechat_channel_id
        3. AI integration is enabled for the channel
        4. Message is from a visitor (not operator, bot, or system)
        """
        if self.channel_type != 'livechat':
            return False

        if not self.livechat_channel_id:
            return False

        if not self.livechat_channel_id.ai_enabled:
            return False

        if not self._is_visitor_message(message):
            return False

        return True

    def _is_visitor_message(self, message):
        """
        Check if a message is from a visitor (not operator, bot, or system).

        Bot exclusion checks come FIRST to prevent response loops,
        then visitor detection follows.

        Exclusions (checked first):
        - The channel's AI bot partner (prevents response loops)
        - The default AI bot partner (fallback)
        - OdooBot (base.partner_root)

        Visitor detection:
        - Message has author_guest_id set (guest visitor, e.g. LINE user)
        - Message has no author_id (anonymous visitor)
        - Author is a partner without any internal user accounts
        """
        # --- Bot exclusion checks first (loop prevention) ---

        # Exclude by author_id (partner-based bots)
        if message.author_id:
            # Exclude the channel's AI bot partner
            if self.livechat_channel_id.ai_bot_partner_id:
                if message.author_id.id == self.livechat_channel_id.ai_bot_partner_id.id:
                    return False

            # Exclude default AI bot partner
            default_bot = self.env.ref(
                'im_livechat_ai.partner_ai_bot', raise_if_not_found=False,
            )
            if default_bot and message.author_id.id == default_bot.id:
                return False

            # Exclude OdooBot
            odoobot = self.env.ref('base.partner_root', raise_if_not_found=False)
            if odoobot and message.author_id.id == odoobot.id:
                return False

        # --- Visitor detection ---

        # Guest-based visitor (e.g. LINE module posts with author_guest_id)
        if message.author_guest_id:
            return True

        # Anonymous visitor (no author)
        if not message.author_id:
            return True

        # Visitors are partners without internal user accounts
        return not message.author_id.user_ids

    def _build_llm_messages(self, livechat_channel):
        """
        Build the messages array for an OpenAI-compatible chat completions API.

        Fetches the most recent N messages from this discuss.channel,
        determines the role for each (user vs assistant), and prepends
        the system prompt.

        Args:
            livechat_channel (im_livechat.channel): The livechat channel config

        Returns:
            list: List of dicts with 'role' and 'content' keys
        """
        max_history = max(1, min(livechat_channel.ai_max_history or 50, 200))

        # Get bot partner ID for role assignment
        bot_partner_id = None
        if livechat_channel.ai_bot_partner_id:
            bot_partner_id = livechat_channel.ai_bot_partner_id.id

        # Also check default bot partner
        default_bot = self.env.ref(
            'im_livechat_ai.partner_ai_bot', raise_if_not_found=False,
        )
        default_bot_id = default_bot.id if default_bot else None

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

        # Build messages array
        llm_messages = []

        # Add system prompt if configured (with optional WELL knowledge)
        effective_prompt = livechat_channel._get_effective_system_prompt()
        if effective_prompt:
            llm_messages.append({
                'role': 'system',
                'content': effective_prompt,
            })

        for msg in recent_messages:
            # Determine role
            is_bot = False
            if msg.author_id:
                if bot_partner_id and msg.author_id.id == bot_partner_id:
                    is_bot = True
                elif default_bot_id and msg.author_id.id == default_bot_id:
                    is_bot = True

            role = 'assistant' if is_bot else 'user'

            # Strip HTML tags and decode HTML entities for clean text
            body = msg.body or ''
            clean_body = html_lib.unescape(_HTML_TAG_RE.sub('', body)).strip()
            if not clean_body:
                continue

            llm_messages.append({
                'role': role,
                'content': clean_body,
            })

        return llm_messages
