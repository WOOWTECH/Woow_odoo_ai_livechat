import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class MailMessage(models.Model):
    """
    Extension of mail.message to route livechat messages to AI threads
    and send AI responses to LINE.

    This ensures that messages posted in livechat channels with AI operators
    are processed by the AI system, similar to how ai_chat messages are handled.
    It also ensures AI responses are forwarded to LINE users.
    """
    _inherit = 'mail.message'

    @api.model_create_multi
    def create(self, vals_list):
        """
        Override create to trigger AI processing for livechat messages
        and send AI responses to LINE.

        When a message is created in a livechat channel that has an AI thread,
        this method ensures the message is routed to the AI for processing.
        AI responses are also sent to LINE if applicable.
        """
        # Skip if this message came from LINE webhook (prevent loop)
        if self.env.context.get('from_line_webhook'):
            return super().create(vals_list)

        messages = super().create(vals_list)

        for message in messages:
            if message.model == 'discuss.channel' and message.message_type == 'comment':
                # Process incoming messages for AI
                self._process_livechat_ai_message(message)
                # Send operator/AI messages to LINE
                self._send_to_line_if_applicable(message)

        return messages

    def _send_to_line_if_applicable(self, message):
        """
        Send message to LINE if applicable.

        This method forwards messages from operators (including AI) to LINE users.
        It checks if the message is in a LINE-enabled livechat channel and
        sends it to the LINE user.

        :param message: mail.message record
        """
        # Only process messages from operators (not from guests)
        if message.author_guest_id or not message.author_id:
            return

        # Check if message is in a discuss.channel
        if message.model != 'discuss.channel' or not message.res_id:
            return

        try:
            discuss_channel = self.env['discuss.channel'].sudo().browse(message.res_id)
            if not discuss_channel.exists():
                return

            # Check if this is a LINE conversation
            if not hasattr(discuss_channel, 'line_user_id') or not discuss_channel.line_user_id:
                return

            # Send to LINE using the channel's method
            if hasattr(discuss_channel, '_notify_line_user'):
                discuss_channel._notify_line_user(message)
                _logger.info(
                    "LINE: Sent message %s to LINE user %s",
                    message.id, discuss_channel.line_user_id
                )
        except Exception as e:
            _logger.error('LINE: Error sending message %s to LINE: %s', message.id, e)

    def _process_livechat_ai_message(self, message):
        """
        Process a message for AI response in livechat channels.

        This method:
        1. Checks if the message is in a livechat channel
        2. Verifies the channel has an associated AI thread
        3. Ensures the message is not from the AI itself
        4. Triggers the AI to generate a response

        :param message: The mail.message record to process
        """
        try:
            channel = self.env['discuss.channel'].browse(message.res_id)

            if not channel.exists() or channel.channel_type != 'livechat':
                return

            # Find the AI thread for this channel
            ai_thread = self.env['ai.thread'].search([
                ('discuss_channel_id', '=', channel.id)
            ], limit=1)

            if not ai_thread:
                _logger.debug(
                    "No AI thread found for livechat channel %s, skipping AI processing",
                    channel.id
                )
                return

            # Don't process messages from the AI itself
            ai_partner_id = ai_thread.ai_partner_id.id if ai_thread.ai_partner_id else False
            if message.author_id.id == ai_partner_id:
                _logger.debug(
                    "Message %s is from AI, skipping AI processing",
                    message.id
                )
                return

            # Extract the message text
            message_body = message.body or ''
            # Strip HTML tags for plain text
            import re
            plain_text = re.sub(r'<[^>]+>', '', message_body).strip()

            if not plain_text:
                _logger.debug(
                    "Message %s has no text content, skipping AI processing",
                    message.id
                )
                return

            _logger.info(
                "Processing livechat message %s for AI thread %s (channel %s)",
                message.id, ai_thread.id, channel.id
            )

            # Use the mail.ai abstract model to trigger AI processing
            # This delegates to the ai_mail_gt module's AI logic
            mail_ai = self.env['mail.ai']
            mail_ai._apply_logic(channel, message)

        except Exception as e:
            _logger.exception(
                "Error processing livechat message %s for AI: %s",
                message.id, str(e)
            )
