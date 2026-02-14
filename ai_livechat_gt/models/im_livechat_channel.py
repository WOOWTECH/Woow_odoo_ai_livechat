from odoo import api, fields, models


class ImLivechatChannel(models.Model):
    _inherit = 'im_livechat.channel'

    user_ids = fields.Many2many(context={'active_test': False})
    available_operator_ids = fields.Many2many(context={'active_test': False})
    ai_assistant_id = fields.Many2one('ai.assistant', string="AI Operators",
                                      compute='_compute_ai_assistant_id', inverse='_inverse_ai_assistant_id')
    ai_context_id = fields.Many2one('ai.context', string="AI Context",
                                    compute='_compute_ai_context_id', store=True, readonly=False,
                                    help="The AI context used for livechat conversations.")

    @api.depends('user_ids.is_ai', 'ai_assistant_id')
    def _compute_available_operator_ids(self):
        super()._compute_available_operator_ids()
        for r in self:
            r.available_operator_ids = r.available_operator_ids | r.ai_assistant_id.with_context(active_test=False).user_id

    @api.depends('user_ids')
    def _compute_ai_assistant_id(self):
        for r in self:
            r.ai_assistant_id = r.with_context(active_test=False).user_ids.ai_assistant_ids[:1]

    def _inverse_ai_assistant_id(self):
        for r in self:
            r.user_ids = r.user_ids.filtered(lambda u: not u.is_ai) | r.ai_assistant_id.with_context(active_test=False).user_id

    @api.depends('ai_assistant_id')
    def _compute_ai_context_id(self):
        for r in self:
            r.ai_context_id = r.ai_assistant_id.context_id

    def _get_operator(self, previous_operator_id=None, lang=None, country_id=None):
        if self.ai_assistant_id:
            return self.ai_assistant_id.with_context(active_test=False).user_id
        return super()._get_operator(previous_operator_id, lang, country_id)
