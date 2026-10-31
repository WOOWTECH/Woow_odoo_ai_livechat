# -*- coding: utf-8 -*-

import logging

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

from .im_livechat_channel import (
    DEFAULT_BASE_URL,
    DEFAULT_LOG_RETENTION_DAYS,
    DEFAULT_MODEL,
    ICP_API_KEY,
    ICP_BASE_URL,
    ICP_LOG_PAYLOAD,
    ICP_LOG_RETENTION_DAYS,
)

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    """
    System-level Live Chat AI settings (LiteLLM connection and log privacy).

    Everything is stored in ir.config_parameter. res.config.settings and
    ir.config_parameter are both restricted to base.group_system, and every
    field below also carries groups='base.group_system'.

    The API key is write-only: it is never loaded back into the form and is
    not stored in the transient res_config_settings table (non-stored field
    whose inverse writes the parameter directly).
    """
    _inherit = 'res.config.settings'

    livechat_ai_base_url = fields.Char(
        string='LiteLLM Base URL',
        config_parameter=ICP_BASE_URL,
        default=DEFAULT_BASE_URL,
        groups='base.group_system',
        help='OpenAI-compatible base URL of the company LiteLLM gateway, ending in /v1.',
    )
    livechat_ai_api_key = fields.Char(
        string='LiteLLM API Key',
        compute='_compute_livechat_ai_api_key',
        inverse='_inverse_livechat_ai_api_key',
        store=False,
        groups='base.group_system',
        help='Leave empty to keep the current key. The saved key is never shown again.',
    )
    livechat_ai_api_key_set = fields.Boolean(
        string='API Key Configured',
        compute='_compute_livechat_ai_api_key',
        groups='base.group_system',
    )
    livechat_ai_model_list = fields.Text(
        string='Available Models',
        compute='_compute_livechat_ai_model_list',
        groups='base.group_system',
    )
    livechat_ai_log_payload = fields.Boolean(
        string='Log Conversation Content',
        config_parameter=ICP_LOG_PAYLOAD,
        groups='base.group_system',
        help='Store the messages sent to and received from the LLM in the API logs. '
             'Keep off unless debugging; when turned off, stored content is erased.',
    )
    livechat_ai_log_retention_days = fields.Integer(
        string='Log Retention (days)',
        config_parameter=ICP_LOG_RETENTION_DAYS,
        default=DEFAULT_LOG_RETENTION_DAYS,
        groups='base.group_system',
        help='API logs older than this are deleted by the daily cleanup job.',
    )

    def _compute_livechat_ai_api_key(self):
        _base_url, api_key = self.env['im_livechat.channel']._get_llm_connection()
        for settings in self:
            # Never send the stored key back to the browser.
            settings.livechat_ai_api_key = False
            settings.livechat_ai_api_key_set = bool(api_key)

    def _inverse_livechat_ai_api_key(self):
        for settings in self:
            new_key = (settings.livechat_ai_api_key or '').strip()
            if new_key:
                self._check_livechat_ai_admin()
                self.env['ir.config_parameter'].sudo().set_param(ICP_API_KEY, new_key)
                _logger.info("Live Chat AI: LiteLLM API key updated by user %s", self.env.uid)
        # Drop the typed key from the cache so web_save's read-back returns
        # the computed (empty) value instead of echoing the key.
        self.invalidate_recordset(['livechat_ai_api_key', 'livechat_ai_api_key_set'])

    def _compute_livechat_ai_model_list(self):
        model_ids = self.env['im_livechat.channel']._get_cached_llm_models()
        text = '\n'.join(model_ids) if model_ids else False
        for settings in self:
            settings.livechat_ai_model_list = text

    def _check_livechat_ai_admin(self):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators can change the Live Chat AI connection.'))

    @api.constrains('livechat_ai_base_url')
    def _check_livechat_ai_base_url(self):
        for settings in self:
            url = (settings.livechat_ai_base_url or '').strip()
            if url and not url.startswith(('http://', 'https://')):
                raise ValidationError(_('LiteLLM Base URL must start with http:// or https://'))

    @api.constrains('livechat_ai_log_retention_days')
    def _check_livechat_ai_log_retention_days(self):
        for settings in self:
            if settings.livechat_ai_log_retention_days < 1:
                raise ValidationError(_('Log retention must be at least 1 day.'))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        # A hand-edited invalid parameter must not block saving Settings.
        if 'livechat_ai_log_retention_days' in res and (res['livechat_ai_log_retention_days'] or 0) < 1:
            res['livechat_ai_log_retention_days'] = DEFAULT_LOG_RETENTION_DAYS
        return res

    def set_values(self):
        ICP = self.env['ir.config_parameter'].sudo()
        payload_was_on = str(ICP.get_param(ICP_LOG_PAYLOAD)).lower() in ('true', '1')
        super().set_values()
        if payload_was_on and not self.livechat_ai_log_payload:
            # Turning content logging off also erases what was captured.
            self.env['llm.api.log'].sudo()._erase_payloads()

    # --- Buttons ---

    def action_livechat_ai_fetch_models(self):
        """Settings button "讀取可用模型": refresh the cached model list."""
        self._check_livechat_ai_admin()
        Channel = self.env['im_livechat.channel']
        try:
            model_ids = Channel._fetch_llm_models()
        except Exception as e:  # noqa: BLE001 - every failure becomes a short notice
            _logger.warning("Live Chat AI: fetching LiteLLM models failed: %s", e, exc_info=True)
            status, reason = Channel._describe_llm_error(e)
            if status:
                message = _('讀取模型失敗（HTTP %(status)s）：%(reason)s', status=status, reason=reason)
            else:
                message = _('讀取模型失敗：%(reason)s', reason=reason)
            return self._livechat_ai_notification(message, 'danger', reload=False)
        if DEFAULT_MODEL not in model_ids:
            message = _('已讀取 %(count)s 個模型（注意：清單中沒有預設模型 %(model)s）。',
                        count=len(model_ids), model=DEFAULT_MODEL)
        else:
            message = _('已讀取 %(count)s 個模型。', count=len(model_ids))
        return self._livechat_ai_notification(message, 'success', reload=True)

    def action_livechat_ai_test_connection(self):
        """Settings button "測試連線": tiny chat completion with the default model."""
        self._check_livechat_ai_admin()
        return self.env['im_livechat.channel']._llm_test_notification(DEFAULT_MODEL)

    def _livechat_ai_notification(self, message, kind, reload=False):
        params = {
            'title': _('Live Chat AI'),
            'message': message,
            'type': kind,
            'sticky': kind != 'success',
        }
        if reload:
            params['next'] = {'type': 'ir.actions.client', 'tag': 'reload'}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': params,
        }
