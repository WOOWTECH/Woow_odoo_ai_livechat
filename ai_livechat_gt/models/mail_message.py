import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class MailMessage(models.Model):
    """
    Extension of mail.message to route livechat messages to AI threads.

    This ensures that messages posted in livechat channels with AI operators
    are processed by the AI system, similar to how ai_chat messages are handled.
    """
    _inherit = 'mail.message'

    @api.model_create_multi
    def create(self, vals_list):
        """
        Override create to trigger AI processing for livechat messages.

        When a message is created in a livechat channel that has an AI thread,
        this method ensures the message is routed to the AI for processing.
        """
        messages = super().create(vals_list)

        for message in messages:
            if message.model == 'discuss.channel' and message.message_type == 'comment':
                self._process_livechat_ai_message(message)

        return messages

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
