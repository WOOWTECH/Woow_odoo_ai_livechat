import logging

from odoo import models

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

        After the parent method posts the AI response to the discuss channel,
        this method checks if the channel is a LINE conversation and sends
        the response to the LINE user.

        :param mail_thread: The thread (discuss.channel) to post to
        :param mail_message: The original user message being responded to
        """
        # Call parent to post the message
        super()._post_message_after_commit(mail_thread, mail_message)

        # Send to LINE if applicable
        if mail_thread._name == 'discuss.channel':
            self._send_ai_response_to_line(mail_thread)

    def _send_ai_response_to_line(self, channel):
        """
        Send the AI response to LINE if the channel is a LINE conversation.

        This method:
        1. Checks if the channel has a LINE user associated
        2. Finds the latest AI message in the channel
        3. Sends it to LINE using the channel's _notify_line_user method

        :param channel: discuss.channel record
        """
        try:
            # Check if this is a LINE conversation
            if not hasattr(channel, 'line_user_id') or not channel.line_user_id:
                return

            # Get the latest message from AI in this channel
            latest_msg = self.env['mail.message'].sudo().search([
                ('res_id', '=', channel.id),
                ('model', '=', 'discuss.channel'),
                ('author_id', '=', self.author_id.id),
            ], order='id desc', limit=1)

            if not latest_msg:
                _logger.warning(
                    "LINE: Could not find AI message to send for channel %s",
                    channel.id
                )
                return

            # Send to LINE using the channel's method
            if hasattr(channel, '_notify_line_user'):
                channel.sudo()._notify_line_user(latest_msg)
                _logger.info(
                    "LINE: Sent AI response (message %s) to LINE user %s",
                    latest_msg.id, channel.line_user_id
                )
        except Exception as e:
            _logger.exception(
                "LINE: Error sending AI response to LINE for channel %s: %s",
                channel.id, str(e)
            )
