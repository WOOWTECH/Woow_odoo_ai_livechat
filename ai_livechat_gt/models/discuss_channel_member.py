from odoo import models
from odoo.addons.mail.tools.discuss import Store


class ChannelMember(models.Model):
    _inherit = 'discuss.channel.member'

    def _to_store(self, store: Store, **kwargs):
        super()._to_store(store, **kwargs)
        for member in self.filtered(lambda m: m.partner_id.is_ai and m.channel_id.channel_type == "livechat"):
            store.add(
                member,
                {
                    "is_bot": True,
                },
            )
