from odoo import models
from odoo.addons.mail.tools.discuss import Store


class ChannelMember(models.Model):
    """
    Extension of discuss.channel.member to mark AI partners as bots in livechat.
    """
    _inherit = 'discuss.channel.member'

    def _to_store(self, store: Store, **kwargs):
        """
        Extend store data to mark AI members as bots in livechat channels.

        This ensures the frontend UI properly identifies and displays AI participants
        with bot indicators in livechat conversations.

        :param store: The Discuss Store object to populate
        :param kwargs: Additional keyword arguments passed to parent method
        """
        super()._to_store(store, **kwargs)

        # Filter AI members in livechat channels with prefetching for performance
        # Prefetch partner_id.is_ai and channel_id.channel_type to avoid N+1 queries
        self.mapped('partner_id.is_ai')
        self.mapped('channel_id.channel_type')

        ai_livechat_members = self.filtered(
            lambda m: m.partner_id.is_ai and m.channel_id.channel_type == "livechat"
        )

        for member in ai_livechat_members:
            store.add(member, {"is_bot": True})
