import logging

from odoo import models, _
from odoo.addons.ai_base_gt.models.tools import ai_tool

_logger = logging.getLogger(__name__)


class AIThread(models.Model):
    _inherit = 'ai.thread'

    @ai_tool(condition=lambda thread: thread.discuss_channel_id.channel_type == 'livechat')
    def _forward_to_human_operator(self) -> dict:
        """
        Find and add a human operator to the current conversation, stop the role of AI and
        allow the visitor to discuss with a real person.

        This AI tool is only available when the discuss channel is of type 'livechat'.
        It uses sudo() to perform cross-user operations required for operator assignment.

        Returns:
            dict: A dictionary containing:
                - status (bool): True if a human operator was successfully assigned
                - operator (dict): Contains 'name' of the operator (only if status is True)
                - error (str): Error message (only if status is False)

        Note:
            The operator 'id' is intentionally not exposed to prevent information disclosure.
        """
        self.ensure_one()

        try:
            # Use sudo() to bypass access controls for cross-user operator assignment
            # This is necessary because the AI user needs to add human operators to channels
            human_operator = self.discuss_channel_id.sudo()._ai_forward_to_human_operator()

            if human_operator:
                return {
                    'status': True,
                    'operator': {
                        # Only expose operator name, not internal ID for security
                        'name': human_operator.livechat_username or human_operator.name,
                    }
                }
            else:
                return {
                    'status': False,
                    'error': _("No human operators available at this time.")
                }

        except Exception as e:
            _logger.exception(
                "Error forwarding AI thread %s to human operator: %s",
                self.id, str(e)
            )
            return {
                'status': False,
                'error': _("An error occurred while connecting to a human operator. Please try again.")
            }
