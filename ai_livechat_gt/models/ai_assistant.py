from odoo import models, fields


class AIAssistant(models.Model):
    _inherit = 'ai.assistant'

    livechat_channel_ids = fields.One2many('im_livechat.channel', 'ai_assistant_id', string="Livechat Channels")
