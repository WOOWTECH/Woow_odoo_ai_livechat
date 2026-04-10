# -*- coding: utf-8 -*-

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    """
    Extend discuss.channel to intercept livechat message events.

    This extension hooks into the message notification pipeline to trigger
    n8n webhooks when visitors send messages in livechat sessions.
    """
    _inherit = 'discuss.channel'

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """
        Override _notify_thread to intercept livechat message events.

        This method is called after each message is posted to the channel.
        We hook into it to trigger n8n webhooks for visitor messages.

        Args:
            message (mail.message): The message record that was posted
            msg_vals (dict, optional): Message values dictionary
            **kwargs: Additional parameters passed to parent

        Returns:
            Recipients data from parent method
        """
        # Always call super first to ensure normal operation
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)

        # Only proceed if this is a livechat channel with n8n enabled
        try:
            if self._should_trigger_n8n_webhook(message):
                self.livechat_channel_id._trigger_n8n_webhook(
                    event_type='message_received',
                    session=self,
                    message=message
                )
        except Exception as e:
            # Log error but don't block normal chat operation
            _logger.warning(
                f"Failed to trigger n8n webhook for channel {self.id}: {e}",
                exc_info=True
            )

        return rdata

    def _should_trigger_n8n_webhook(self, message):
        """
        Determine if n8n webhook should be triggered for this message.

        Checks:
        1. Channel type is livechat
        2. Channel has associated livechat_channel_id
        3. N8N integration is enabled for the channel
        4. Message is from visitor (not operator or bot)

        Args:
            message (mail.message): The message to check

        Returns:
            bool: True if webhook should be triggered
        """
        # Check if this is a livechat channel
        if self.channel_type != 'livechat':
            return False

        # Check if channel has livechat configuration
        if not self.livechat_channel_id:
            return False

        # Check if n8n integration is enabled
        if not self.livechat_channel_id.n8n_enabled:
            return False

        # Check if message is from visitor
        if not self._is_visitor_message(message):
            return False

        return True

    def _is_visitor_message(self, message):
        """
        Check if message is from visitor (not operator or bot).

        A visitor message is one where:
        - Message has no author (anonymous visitor), OR
        - Author is a partner without any internal user accounts

        Args:
            message (mail.message): The message to check

        Returns:
            bool: True if message is from visitor
        """
        # Anonymous visitor (no author)
        if not message.author_id:
            return True

        # Check if author has any internal user accounts
        # Visitors are partners without user_ids
        return not message.author_id.user_ids
