import logging

from markupsafe import Markup
from odoo import api, models, _
from odoo.addons.ai_base_gt.models.tools import after_commit

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    @api.model_create_multi
    def create(self, vals_list):
        """
        Override create to automatically create ai.thread for livechat channels
        that have an AI operator assigned.
        """
        channels = super().create(vals_list)

        # Create ai.thread for livechat channels with AI operators
        for channel in channels:
            if channel.channel_type == 'livechat':
                channel._create_ai_thread_if_needed()

        return channels

    def _create_ai_thread_if_needed(self):
        """
        Create an ai.thread for this livechat channel if:
        1. The channel has a livechat_channel_id with an ai_assistant_id configured
        2. No ai.thread already exists for this channel

        :return: The created ai.thread record, or False if not created
        """
        self.ensure_one()

        if self.channel_type != 'livechat':
            return False

        # Check if ai.thread already exists for this channel
        existing_thread = self.env['ai.thread'].search([
            ('discuss_channel_id', '=', self.id)
        ], limit=1)

        if existing_thread:
            _logger.debug(
                "AI thread already exists for livechat channel %s (thread_id=%s)",
                self.id, existing_thread.id
            )
            return existing_thread

        # Check if the livechat channel has an AI assistant configured
        livechat_channel = self.livechat_channel_id
        if not livechat_channel or not livechat_channel.ai_assistant_id:
            _logger.debug(
                "No AI assistant configured for livechat channel %s",
                self.id
            )
            return False

        ai_assistant = livechat_channel.ai_assistant_id

        try:
            thread = self.env['ai.thread'].create({
                'name': self.name or f'Livechat {self.id}',
                'assistant_id': ai_assistant.id,
                'discuss_channel_id': self.id,
            })
            _logger.info(
                "Created AI thread %s for livechat channel %s with assistant %s",
                thread.id, self.id, ai_assistant.name
            )
            return thread
        except Exception as e:
            _logger.exception(
                "Failed to create AI thread for livechat channel %s: %s",
                self.id, str(e)
            )
            return False

    def _ai_forward_to_human_operator(self):
        """
        Forward a livechat conversation from AI to a human operator.

        This method handles the transition from AI-assisted chat to human support by:
        1. Finding an available human operator from the livechat channel
        2. Adding the human operator to the conversation
        3. Updating the channel name to reflect the new operator
        4. Posting a notification about the operator joining

        Similar to _process_step_forward_operator in chatbot_script_step.py but adapted for AI.

        :return: The human operator record (res.users) if found and successfully added,
                 otherwise False
        :rtype: recordset or bool
        """
        self.ensure_one()
        livechat_channel = self.livechat_channel_id

        if not livechat_channel:
            _logger.warning(
                "Cannot forward to human operator: no livechat channel found for discuss channel %s",
                self.id
            )
            return False

        # Filter out AI operators to get only human operators
        human_operators = livechat_channel.available_operator_ids.filtered(
            lambda user: not user.is_ai
        )

        if not human_operators:
            _logger.info(
                "No human operators available for livechat channel %s (discuss channel %s)",
                livechat_channel.id, self.id
            )
            return False

        human_operator = human_operators[0]

        try:
            # Add the human operator to the channel members
            self.add_members(
                human_operator.partner_id.ids,
                open_chat_window=True,
                post_joined_message=False
            )

            # Update channel name: replace AI operator name with human operator name
            # Only modify if we have a current livechat_operator_id to replace
            if self.livechat_operator_id:
                ai_operator_name = self.livechat_operator_id.name
                human_operator_name = human_operator.livechat_username or human_operator.name

                # Find the position of AI operator name in the channel name
                ai_name_position = self.name.rfind(ai_operator_name)

                if ai_name_position != -1:
                    # AI operator name found - replace it with human operator name
                    new_name = self.name[:ai_name_position].strip() + " " + human_operator_name
                else:
                    # AI operator name not found - append human operator name
                    new_name = self.name.strip() + " " + human_operator_name
                    _logger.debug(
                        "AI operator name '%s' not found in channel name '%s', appending human operator name",
                        ai_operator_name, self.name
                    )

                # Validate and set the new channel name (max 256 chars is typical for Odoo char fields)
                self.name = new_name[:256] if len(new_name) > 256 else new_name

            # Update the livechat operator to the human operator
            self.livechat_operator_id = human_operator.partner_id
            self._post_joined_message_after_commit(human_operator)

            _logger.info(
                "Successfully forwarded discuss channel %s to human operator %s (user_id=%s)",
                self.id, human_operator.name, human_operator.id
            )
            return human_operator

        except Exception as e:
            _logger.exception(
                "Failed to forward discuss channel %s to human operator: %s",
                self.id, str(e)
            )
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
