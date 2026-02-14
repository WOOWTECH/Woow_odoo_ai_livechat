from odoo import models, _
from odoo.addons.ai_base_gt.models.tools import ai_tool


class AIThread(models.Model):
    _inherit = 'ai.thread'

    @ai_tool(condition=lambda thread: thread.discuss_channel_id.channel_type == 'livechat')
    def _forward_to_human_operator(self) -> dict:
        """
        Find and add a human operator to the current conversation, stop the role of AI and
        allow the visitor to discuss with a real person.

        Returns:
            dict: A dictionary containing the status of the operation and either
                  the human operator's details or an error message.
        """
        self.ensure_one()
        human_operator = self.discuss_channel_id.sudo()._ai_forward_to_human_operator()
        if human_operator:
            return {
                'status': True,
                'operator': {
                    'id': human_operator.id,
                    'name': human_operator.name,
                }
            }
        else:
            return {
                'status': False,
                'error': _("No human operators available at this time.")
            }
