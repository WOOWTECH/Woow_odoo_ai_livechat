from markupsafe import Markup
from odoo import models, _
from odoo.addons.ai_base_gt.models.tools import after_commit


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    def _ai_forward_to_human_operator(self):
        """
        Method to forward a livechat conversation from AI to a human operator.
        Similar to _process_step_forward_operator in chatbot_script_step.py but adapted for AI.

        :return: The human operator record if found, otherwise False
        """
        self.ensure_one()
        livechat_channel = self.livechat_channel_id

        if not livechat_channel:
            return False

        human_operators = livechat_channel.available_operator_ids.filtered(
            lambda user: not user.is_ai
        )

        if human_operators:
            human_operator = human_operators[0]

            self.add_members(
                human_operator.partner_id.ids,
                open_chat_window=True,
                post_joined_message=False
            )
            # Rename the channel to include the operator's name
            ai_operator_name = self.livechat_operator_id.name
            human_operator_name = human_operator.livechat_username or human_operator.name
            new_name = self.name[:self.name.rfind(ai_operator_name)].strip() + " " + human_operator_name
            self.name = new_name

            self.livechat_operator_id = human_operator.partner_id
            self._post_joined_message_after_commit(human_operator)
            return human_operator

        return False

    @after_commit(wait=True)
    def _post_joined_message_after_commit(self, human_operator):
        """Post the joined message to the channel"""
        self.ensure_one()
        self.message_post(
            body=Markup('<div class="o_mail_notification">%s</div>') %
            _('%s has joined', human_operator.livechat_username or human_operator.partner_id.name),
            message_type='notification',
            subtype_xmlid='mail.mt_comment'
        )
        self._broadcast(human_operator.partner_id.ids)
        self.channel_pin(pinned=True)
