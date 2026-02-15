from odoo import api, fields, models


class ImLivechatChannel(models.Model):
    """
    Extension of im_livechat.channel to support AI-powered operators.

    This model adds the ability to assign AI assistants as operators for livechat channels,
    allowing automated AI responses while maintaining the option to escalate to human operators.
    """
    _inherit = 'im_livechat.channel'

    # Override context to include inactive users (AI users may be archived)
    user_ids = fields.Many2many(context={'active_test': False})
    available_operator_ids = fields.Many2many(context={'active_test': False})

    ai_assistant_id = fields.Many2one(
        'ai.assistant',
        string="AI Operators",
        compute='_compute_ai_assistant_id',
        inverse='_inverse_ai_assistant_id',
        help="The AI assistant assigned to handle conversations on this livechat channel. "
             "When set, the AI will be the first responder for new conversations."
    )
    ai_context_id = fields.Many2one(
        'ai.context',
        string="AI Context",
        compute='_compute_ai_context_id',
        store=True,
        readonly=False,
        help="The AI context used for livechat conversations. "
             "Defines the AI's behavior, personality, and knowledge base."
    )

    @api.depends('user_ids.is_ai', 'ai_assistant_id')
    def _compute_available_operator_ids(self):
        """
        Extend available operators to include AI assistant users.

        This ensures the AI assistant's user is always included in the available
        operator pool, even if the user is archived (active=False).
        """
        super()._compute_available_operator_ids()
        for record in self:
            ai_user = record.ai_assistant_id.with_context(active_test=False).user_id
            if ai_user:
                record.available_operator_ids = record.available_operator_ids | ai_user

    @api.depends('user_ids')
    def _compute_ai_assistant_id(self):
        """
        Compute the AI assistant from the channel's user assignments.

        Finds the first AI assistant associated with any AI user assigned to this channel.
        Uses active_test=False to include archived AI users.
        """
        for record in self:
            ai_assistants = record.with_context(active_test=False).user_ids.ai_assistant_ids
            record.ai_assistant_id = ai_assistants[:1] if ai_assistants else False

    def _inverse_ai_assistant_id(self):
        """
        Update channel users when AI assistant is changed.

        Removes any existing AI users and adds the new AI assistant's user to the channel.
        Preserves all non-AI (human) operator assignments.
        """
        for record in self:
            # Keep only non-AI users, then add the new AI assistant's user
            human_users = record.user_ids.filtered(lambda u: not u.is_ai)
            ai_user = record.ai_assistant_id.with_context(active_test=False).user_id
            record.user_ids = human_users | ai_user if ai_user else human_users

    @api.depends('ai_assistant_id')
    def _compute_ai_context_id(self):
        """
        Derive AI context from the assigned AI assistant.

        The context defines the AI's behavior and is inherited from the assistant's
        default context configuration.
        """
        for record in self:
            record.ai_context_id = record.ai_assistant_id.context_id if record.ai_assistant_id else False

    def _get_operator(self, previous_operator_id=None, lang=None, country_id=None):
        """
        Override operator selection to prioritize AI assistant.

        When an AI assistant is configured for this channel, it will always be
        returned as the operator, bypassing the normal operator selection logic.
        This ensures AI handles initial customer contact before any human escalation.

        :param previous_operator_id: ID of the previous operator (unused when AI is configured)
        :param lang: Preferred language (unused when AI is configured)
        :param country_id: Customer's country ID (unused when AI is configured)
        :return: The AI assistant's user record, or result from parent method
        :rtype: res.users recordset
        """
        if self.ai_assistant_id:
            return self.ai_assistant_id.with_context(active_test=False).user_id
        return super()._get_operator(previous_operator_id, lang, country_id)
