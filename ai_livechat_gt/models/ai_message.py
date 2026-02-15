import logging

from odoo import models, Command

_logger = logging.getLogger(__name__)


class AIMessageLivechat(models.Model):
    """
    Extension of ai.message to send AI responses to LINE.

    This ensures that when AI generates a response in a livechat channel
    that is connected to LINE, the response is automatically forwarded
    to the LINE user.
    """
    _inherit = 'ai.message'

    def _post_message_after_commit(self, mail_thread, mail_message):
        """
        Override to send AI responses to LINE after posting.

        This method overrides the parent to capture the created message
        and send it to LINE immediately after posting.

        :param mail_thread: The thread (discuss.channel) to post to
        :param mail_message: The original user message being responded to
        """
        # Replicate parent logic but capture the response message
        if mail_thread._name != 'discuss.channel':
            partner_ids = (mail_message.author_id | mail_message.partner_ids).ids
        else:
            partner_ids = None

        # Post AI response to the thread
        response_msg = mail_thread.with_user(self.thread_id.ai_user_id).with_context(
            mail_create_nosubscribe=True
        ).message_post(
            body=self.content_html,
            author_id=self.author_id.id,
            message_type=mail_message.message_type,
            subtype_id=mail_message.subtype_id.id,
            partner_ids=partner_ids,
        )
        response_msg.ai_message_ids = [Command.link(self.id)]

        # Send to LINE if applicable - use the actual response message
        if mail_thread._name == 'discuss.channel':
            self._send_ai_response_to_line(mail_thread, response_msg)

    def _send_ai_response_to_line(self, channel, response_msg):
        """
        Send the AI response to LINE if the channel is a LINE conversation.

        :param channel: discuss.channel record
        :param response_msg: The mail.message record that was just created
        """
        try:
            # Check if this is a LINE conversation
            if not hasattr(channel, 'line_user_id') or not channel.line_user_id:
                return

            if not response_msg:
                _logger.warning(
                    "LINE: No response message to send for channel %s",
                    channel.id
                )
                return

            # Send to LINE using the channel's method
            if hasattr(channel, '_notify_line_user'):
                channel.sudo()._notify_line_user(response_msg)
                _logger.info(
                    "LINE: Sent AI response (message %s) to LINE user %s",
                    response_msg.id, channel.line_user_id
                )
        except Exception as e:
            _logger.exception(
                "LINE: Error sending AI response to LINE for channel %s: %s",
                channel.id, str(e)
            )
