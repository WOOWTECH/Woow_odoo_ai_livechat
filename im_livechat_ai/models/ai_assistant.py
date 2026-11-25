# -*- coding: utf-8 -*-
"""AI assistants of Live Chat AI (「AI 助理」): the persona that answers Live
Chat visitors, and the bot partner it posts as.

The assistant holds the persona (name visitors see, avatar, bot partner,
system prompt, error message, history length); where its brain is and how it
is called is its AI connection (``im_livechat.ai.connection``), readable by
system administrators only. A system administrator ticks "Available in Live
Chat" (「可用在 Live Chat」) on the assistants Live Chat may use; Live Chat
managers pick one of those on a channel's 「AI 助理」 tab. They read the name,
the description and whether its connection is safe for customers only.

The bot partner stays archived (like Odoo's chatbot partners): it is never
offered in "Start a conversation" nor in mention suggestions, yet it posts in
the Live Chat sessions. It is tagged 「AI 機器人」, so messages of a former
bot partner still count as bot turns.

Aligned with Discuss AI and Project AI; nothing is shared with those modules.
"""

import base64
import logging
from datetime import timedelta

from odoo import api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from .ai_connection import NO_IN_USE_CHECK, READINESS_OK, SYSTEM_GROUP

_logger = logging.getLogger(__name__)

DEFAULT_MAX_HISTORY = 50
MAX_MAX_HISTORY = 200
BOT_CATEGORY_XMLID = 'im_livechat_ai.partner_category_ai_bot'
DEFAULT_BOT_XMLID = 'im_livechat_ai.partner_ai_bot'
BOT_AVATAR_PATH = 'im_livechat_ai/static/img/ai_bot.png'
RECOVERY_CRON_XMLID = 'im_livechat_ai.ir_cron_recover_ai_jobs'
# Reply job monitoring: a recovery cron that has not run for this long, or
# unfinished jobs this old, are reported (administrators, assistant form).
MONITOR_STALE_MINUTES = 5


def bot_avatar():
    """Base64 avatar of the AI bot partners (shipped with the module)."""
    with tools.file_open(BOT_AVATAR_PATH, 'rb') as avatar:
        return base64.b64encode(avatar.read())


class LivechatAiAssistant(models.Model):
    _name = 'im_livechat.ai.assistant'
    _description = 'Live Chat AI Assistant'
    _inherit = ['image.mixin']
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True, translate=False,
                       help='The name visitors see as the author of the AI replies.')
    description = fields.Text(string='Description',
                              help='Shown to Live Chat managers when they choose the AI assistant of a channel.')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    partner_id = fields.Many2one(
        'res.partner', string='Bot Partner', readonly=True, copy=False, ondelete='restrict', index=True,
        help='Partner the AI posts as. Created automatically and kept archived; name and avatar follow the AI '
             'assistant.',
    )
    connection_id = fields.Many2one(
        'im_livechat.ai.connection', string='AI Connection', required=True, ondelete='restrict', index=True,
        groups=SYSTEM_GROUP,
        help='Where the brain of this assistant is and how it is called: backend type, URL, key, model, '
             'timeout. Several assistants may share one connection.',
    )
    livechat_available = fields.Boolean(
        string='Available in Live Chat', groups=SYSTEM_GROUP, copy=False,
        help='Live Chat managers can choose this AI assistant for their channels only while it is ticked. '
             'Every Live Chat channel can be reached by anonymous visitors, so tick it only for assistants '
             'meant to answer the public.')
    livechat_selectable = fields.Boolean(
        string='Selectable in Live Chat', compute='_compute_livechat_selectable',
        search='_search_livechat_selectable',
        help='Active and available in Live Chat: a Live Chat manager may choose it for a channel.')
    customer_safe = fields.Boolean(
        string='Connection Safe for Customers', compute='_compute_customer_safe',
        search='_search_customer_safe',
        help='Its AI connection is safe for customers. Read with administrator rights; only the yes / no is '
             'shown.')

    # --- Persona (system administrators only) ---
    system_prompt = fields.Text(string='System Prompt', groups=SYSTEM_GROUP,
                                help='Optional. Leave empty to let the backend decide the role.')
    max_history = fields.Integer(string='History Messages', default=DEFAULT_MAX_HISTORY, groups=SYSTEM_GROUP,
                                 help='Most recent messages of the conversation sent as context (1-200).')
    error_message = fields.Text(
        string='Error Message', groups=SYSTEM_GROUP,
        help='Posted to the visitor when the backend fails. Empty: a default message in the visitor\'s '
             'language.')
    livechat_channel_ids = fields.One2many('im_livechat.channel', 'ai_assistant_id', string='Used By Channels',
                                           groups=SYSTEM_GROUP, readonly=True)
    call_log_count = fields.Integer(compute='_compute_call_log_count', groups=SYSTEM_GROUP)
    job_runner_info = fields.Text(string='Reply Processing', compute='_compute_job_runner_info',
                                  groups=SYSTEM_GROUP,
                                  help='How the replies of the Live Chat AI are processed, and stuck reply jobs '
                                       '(all channels of this database).')
    job_runner_warning = fields.Boolean(compute='_compute_job_runner_info', groups=SYSTEM_GROUP)

    _sql_constraints = [
        ('partner_uniq', 'unique(partner_id)', 'Each bot partner can belong to one AI assistant only.'),
    ]

    def _check_admin(self):
        # (a request without a user, uid None: nobody)
        if not self.env.su and not (self.env.uid and self.env.user.has_group(SYSTEM_GROUP)):
            raise AccessError(self.env._('Only system administrators can change AI assistants.'))

    def action_test_connection(self):
        """Test this assistant's AI connection (the n8n ping names the assistant)."""
        self.ensure_one()
        self._check_admin()
        return self.sudo().connection_id._test_connection(assistant=self.sudo())

    # --- Computed (read with administrator rights, only yes / no shown) ---

    def _compute_livechat_selectable(self):
        # sudo: livechat_available is for system administrators; the yes / no is not sensitive
        for assistant in self:
            record = assistant.sudo()
            assistant.livechat_selectable = bool(record.active and record.livechat_available)

    def _search_livechat_selectable(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(self.env._('This field can only be searched as equal to True or False.'))
        # sudo: evaluated as administrator, only ids come back
        selectable = self.sudo().search([('active', '=', True), ('livechat_available', '=', True)])
        positive = (operator == '=') == value
        return [('id', 'in' if positive else 'not in', selectable.ids)]

    def _compute_customer_safe(self):
        # sudo: im_livechat.ai.connection - only system administrators read connections
        for assistant in self:
            assistant.customer_safe = bool(assistant.sudo().connection_id.customer_safe)

    def _search_customer_safe(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(self.env._('This field can only be searched as equal to True or False.'))
        safe = self.sudo().with_context(active_test=False).search([('connection_id.customer_safe', '=', True)])
        positive = (operator == '=') == value
        return [('id', 'in' if positive else 'not in', safe.ids)]

    @api.depends('name')
    def _compute_display_name(self):
        # the same name on another active assistant: add the id, so a manager
        # picking one knows which is which (visitors only see the partner's name)
        names = [assistant.name for assistant in self if assistant.name]
        duplicates = set()
        if names:
            data = self.sudo().with_context(active_test=True)._read_group(
                [('name', 'in', names)], ['name'], ['__count'])
            duplicates = {name for name, count in data if count > 1}
        for assistant in self:
            name = assistant.name or ''
            if name in duplicates and isinstance(assistant.id, int):
                name = '%s (#%s)' % (name, assistant.id)
            assistant.display_name = name

    # --- Problems (why the assistant cannot answer) ---

    def _problems(self):
        """Fixed sentences why this assistant cannot answer Live Chat visitors
        now (empty: it can). Never a URL, a key or a connection name."""
        self.ensure_one()
        assistant = self.sudo()
        _ = self.env._
        problems = []
        if not assistant.active:
            problems.append(_('The AI assistant is archived.'))
        if not assistant.livechat_available:
            problems.append(_('The AI assistant is not available in Live Chat.'))
        connection = assistant.connection_id
        if not connection:
            problems.append(_('The AI assistant has no AI connection.'))
        elif connection.readiness != READINESS_OK:
            problems += connection._readiness_reasons()
        return problems

    # --- Constraints ---

    @api.constrains('max_history')
    def _check_limits(self):
        for assistant in self.sudo():
            if not 1 <= assistant.max_history <= MAX_MAX_HISTORY:
                raise ValidationError(self.env._('History messages must be between 1 and 200.'))

    @api.constrains('partner_id')
    def _check_partner(self):
        for assistant in self.sudo():
            if assistant.partner_id and assistant.partner_id.with_context(active_test=False).user_ids:
                raise ValidationError(self.env._('The bot partner of an AI assistant cannot be a user.'))

    # --- The "in use" guard ---

    def _in_use_channels(self):
        ids = [i for i in self.ids if isinstance(i, int)]
        if not ids:
            return self.env['im_livechat.channel'].sudo()
        return self.env['im_livechat.channel'].sudo().search([
            ('ai_enabled', '=', True), ('ai_assistant_id', 'in', ids)])

    def _in_use_snapshot(self):
        if self.env.context.get(NO_IN_USE_CHECK):
            return {}
        used = set(self._in_use_channels().ai_assistant_id.ids)
        return {assistant.id: True for assistant in self.sudo()
                if assistant.id in used and not assistant._problems()}

    def _check_in_use(self, snapshot):
        if not snapshot:
            return
        assistants = self.sudo().browse(list(snapshot))
        assistants.invalidate_recordset()
        broken = assistants.filtered(lambda a: a._problems())
        if not broken:
            return
        channel = broken._in_use_channels()[:1]
        raise UserError(self.env._(
            'The Live Chat channel "%(name)s" answers visitors with this AI assistant. Turn the AI of that '
            'channel off first, or choose another AI assistant for it.', name=channel.name or '?'))

    # --- Bot partner ---

    def _partner_vals(self):
        self.ensure_one()
        category = self.env.ref(BOT_CATEGORY_XMLID, raise_if_not_found=False)
        vals = {
            'name': self.name,
            # archived, like Odoo's chatbot partners: no contact list entry, no mention suggestion
            'active': False,
            'is_company': False,
            'type': 'contact',
            'image_1920': self.image_1920 or bot_avatar(),
        }
        if category:
            vals['category_id'] = [(4, category.id)]
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        assistants = super().create(vals_list)
        for assistant in assistants:
            if not assistant.partner_id:
                partner = self.env['res.partner'].sudo().create(assistant._partner_vals())
                assistant.sudo().partner_id = partner
        return assistants

    def write(self, vals):
        guarded = {}
        if {'active', 'livechat_available', 'connection_id'} & set(vals):
            guarded = self._in_use_snapshot()
        res = super().write(vals)
        if {'name', 'image_1920'} & set(vals):
            for assistant in self.sudo():
                partner = assistant.partner_id.with_context(active_test=False)
                if partner:
                    partner_vals = {'name': assistant.name}
                    if 'image_1920' in vals:
                        partner_vals['image_1920'] = assistant.image_1920 or bot_avatar()
                    partner.write(partner_vals)
        if 'connection_id' in vals:
            # a channel's 18.0.1.x key copy belongs to its old connection (rollback)
            channels = self.sudo().with_context(active_test=False).livechat_channel_ids
            self.env['im_livechat.ai.connection']._drop_legacy_channel_keys(channel_ids=channels.ids)
        self._check_in_use(guarded)
        return res

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for assistant, vals in zip(self, vals_list):
            vals['name'] = self.env._('%s (copy)', assistant.name)
            vals.pop('partner_id', None)
        return vals_list

    # --- Languages and default texts ---

    @api.model
    def _installed_lang(self, *codes):
        """First of `codes` that is an installed language, else the current
        language or en_US (translating with an uninstalled code raises)."""
        installed = {code for code, _name in self.env['res.lang'].get_installed()}
        for code in codes + (self.env.context.get('lang'),):
            if code and code in installed:
                return code
        return 'en_US'

    def _error_text(self, lang=None):
        """The text the visitor gets when the AI cannot answer: the assistant's
        error message, or the default one in `lang`."""
        record = self.sudo()[:1]
        if record and (record.error_message or '').strip():
            return record.error_message
        env = self.with_context(lang=self._installed_lang(lang)).env
        return env._('Sorry, the AI assistant cannot answer right now. Please try again later, or leave your '
                     'contact details and our team will get back to you as soon as possible.')

    # --- Reply processing (administrators) ---

    @api.depends_context('uid')
    def _compute_job_runner_info(self):
        env = self.env
        _ = env._
        cron = env.ref(RECOVERY_CRON_XMLID, raise_if_not_found=False)
        cron = cron.sudo() if cron else cron
        lastcall = cron.lastcall if cron else False
        stale = env['im_livechat.ai.job']._stale_job_count(MONITOR_STALE_MINUTES)
        lines = [_('Replies are processed in a background thread of the web worker that received the visitor '
                   'message; the scheduled action "Live Chat AI: Recover Reply Jobs" (every 2 minutes) sends '
                   'the replies left behind by a restart or a deploy, and the replies due after an unanswered '
                   'handoff wait. A cron process must be running.')]
        warning = False
        if not cron or not cron.active:
            warning = True
            lines.append(_('The scheduled action "Live Chat AI: Recover Reply Jobs" is disabled: replies left '
                           'behind by a restart or a deploy are not sent.'))
        elif not lastcall or lastcall < fields.Datetime.now() - timedelta(minutes=MONITOR_STALE_MINUTES):
            warning = True
            lines.append(_('The scheduled action "Live Chat AI: Recover Reply Jobs" has not run for more than '
                           '%(minutes)s minutes. Check that the Odoo cron process (max_cron_threads of at least '
                           '1) is running: the replies after a restart or a deploy and the replies after a '
                           'handoff wait depend on it.', minutes=MONITOR_STALE_MINUTES))
        else:
            lines.append(_('Last run of the scheduled action: %(time)s (UTC).', time=lastcall))
        if stale:
            warning = True
            lines.append(_('%(count)s reply job(s) of the Live Chat AI have been unfinished for more than '
                           '%(minutes)s minutes: see Live Chat > Configuration > Reply Jobs.',
                           count=stale, minutes=MONITOR_STALE_MINUTES))
        text = '\n'.join(lines)
        for assistant in self:
            assistant.job_runner_info = text
            assistant.job_runner_warning = warning

    # --- Call logs ---

    def _compute_call_log_count(self):
        data = self.env['llm.api.log'].sudo()._read_group(
            [('assistant_id', 'in', self.ids)], ['assistant_id'], ['__count'])
        counts = {assistant.id: count for assistant, count in data}
        for assistant in self:
            assistant.call_log_count = counts.get(assistant.id, 0)

    def action_view_call_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('im_livechat_ai.llm_api_log_action')
        action['domain'] = [('assistant_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_open_bot_partner(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'res_id': self.partner_id.id,
            'view_mode': 'form',
            'context': {'active_test': False},
        }
