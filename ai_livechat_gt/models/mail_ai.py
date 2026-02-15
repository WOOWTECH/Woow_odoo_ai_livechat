from odoo import models


class MailAI(models.AbstractModel):
    """
    Extension of mail.ai to support AI detection in livechat channels.
    """
    _inherit = 'mail.ai'

    # Maximum number of members for a channel to be considered private (visitor + operator)
    _PRIVATE_LIVECHAT_MAX_MEMBERS = 2

    def _is_private_livechat(self, record):
        """
        Determine if the record is a private livechat channel.

        A private livechat is defined as a livechat channel that:
        1. Has at most 2 members (typically visitor + single operator)
        2. Contains no messages from parties outside the channel membership
           (indicating no other operators have been involved)

        :param record: A discuss.channel record to check
        :return: True if the channel is a private livechat, False otherwise
        :rtype: bool
        """
        if not record.channel_member_ids:
            return False

        member_count = len(record.channel_member_ids)
        if member_count > self._PRIVATE_LIVECHAT_MAX_MEMBERS:
            return False

        # Check if any message is from someone not in the channel
        channel_partner_ids = record.channel_member_ids.partner_id
        has_external_messages = record.message_ids.filtered(
            lambda msg: msg.author_id and msg.author_id not in channel_partner_ids
        )
        return not has_external_messages

    def _is_ai_in_private_channel(self, record):
        """
        Check if AI is participating in a livechat channel.

        Extends the parent method to handle livechat-specific logic for
        detecting AI participation in conversations. For livechat channels,
        we check if an AI partner is in the channel members, regardless of
        the "private" status (member count).

        :param record: A discuss.channel record to check
        :return: List of AI partner IDs if AI is in the channel, otherwise delegates to parent
        :rtype: list or result from parent method
        """
        if record._name != 'discuss.channel' or record.channel_type != 'livechat':
            return super()._is_ai_in_private_channel(record)

        # For livechat channels, always check if AI is a member
        # This handles both private (2 members) and group (3+ members) livechats
        ai_partner_ids = self._get_ai_partner_ids()
        channel_partner_ids = record.with_context(active_test=False).channel_partner_ids.ids
        ai_in_channel = list(set(ai_partner_ids) & set(channel_partner_ids))

        if ai_in_channel:
            return ai_in_channel

        # Fallback to parent implementation if no AI found in livechat
        return super()._is_ai_in_private_channel(record)
