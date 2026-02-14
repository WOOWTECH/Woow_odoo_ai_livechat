from odoo import models


class MailAI(models.AbstractModel):
    _inherit = 'mail.ai'

    def _is_private_livechat(self, record):
        """
        Determine if the record is a private livechat. A private livechat is a livechat channel
        that have only 2 members and does not have any message which is not from a member of
        that channel (other operators have not involved).
        """
        return len(record.channel_member_ids) <= 2 and not record.message_ids.filtered(
            lambda msg: msg.author_id and msg.author_id not in record.channel_member_ids.partner_id
        )

    def _is_ai_in_private_channel(self, record):
        ai_partner_ids = self._get_ai_partner_ids()
        if record._name == 'discuss.channel' and record.channel_type == 'livechat' \
                and self._is_private_livechat(record):
            return list(set(ai_partner_ids) & set(record.with_context(active_test=False).channel_partner_ids.ids))
        return super()._is_ai_in_private_channel(record)
