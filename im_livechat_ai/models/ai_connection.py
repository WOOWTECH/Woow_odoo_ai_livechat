# -*- coding: utf-8 -*-
"""AI connections of Live Chat AI (「AI 連線」): where the brain of an AI
assistant is, and how it is called.

A connection holds the backend type (LiteLLM, called with the OpenAI chat
completions API, or an n8n webhook), its URL and key, the model and the call
settings (timeout, retries, documents, content logging). AI assistants
(``im_livechat.ai.assistant``) are the personas that Live Chat channels pick;
each one points to one connection, and several assistants may share it. The
stored key of LiteLLM stays ``openai`` (the 18.0.1.x value, so the upgrade and
a rollback need no conversion).

Only system administrators can read or change connections (ACL: no other
group has access). Runtime reads go through sudo. The API key is write-only:
it lives in an SQL-only column (``api_key_secret``, not an ORM field), so no
read, search, export or form ever returns it.

"Safe for Customers" (``customer_safe``): always for LiteLLM (Odoo sends the
conversation and never any tool: chat-only); for n8n only once a system
administrator ticked it, cleared again when the workflow URL or the backend
type changes. Live Chat visitors are mostly anonymous customers, so the
channel's AI tab warns while its assistant's connection is not safe.

Aligned with Discuss AI and Project AI (field names and behaviour); nothing is
shared with those modules.
"""

import logging

import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import sql

from ..tools.backend import (
    BACKEND_N8N,
    BACKEND_OPENAI,
    endpoint_origin,
    endpoint_url_allowed,
    json_dumps,
    n8n_signature_headers,
    normalize_openai_url,
)

_logger = logging.getLogger(__name__)

# Timeout of one answer (seconds), retries included: Live Chat keeps its own
# range and defaults (LiteLLM 45 s, n8n 120 s: the n8n smart home advisor took
# 112 s on a long question). A timed-out call is never retried.
LITELLM_DEFAULT_TIMEOUT = 45
N8N_DEFAULT_TIMEOUT = 120
MIN_TIMEOUT = 10
MAX_TIMEOUT = 300
DEFAULT_MAX_RETRIES = 1
MAX_MAX_RETRIES = 2
CONNECT_TIMEOUT = 10  # seconds to open the connection (a connect timeout is "not sent": retried)
TEST_TIMEOUT = 15  # seconds, Test Connection

READINESS_OK = 'ok'
READINESS_BLOCKED = 'blocked'

SYSTEM_GROUP = 'base.group_system'
# Legacy 18.0.1.x column on im_livechat_channel that still holds the channel's key
# for a rollback (kept for one version, see migrations/18.0.2.0.0)
LEGACY_TOKEN_COLUMN = 'ai_api_token_secret'
# Context key of internal writes that must not run the in-use guard (migration, tests)
NO_IN_USE_CHECK = 'im_livechat_ai_no_in_use_check'


def default_timeout(backend_type):
    return N8N_DEFAULT_TIMEOUT if backend_type == BACKEND_N8N else LITELLM_DEFAULT_TIMEOUT


class LivechatAiConnection(models.Model):
    _name = 'im_livechat.ai.connection'
    _description = 'Live Chat AI Connection'
    _order = 'name, id'

    name = fields.Char(string='Name', required=True)
    active = fields.Boolean(default=True)
    description = fields.Text(string='Description',
                              help='For administrators: what this backend is and who runs it.')
    backend_type = fields.Selection(
        [(BACKEND_OPENAI, 'LiteLLM (OpenAI compatible)'), (BACKEND_N8N, 'n8n webhook')],
        string='Backend', required=True, default=BACKEND_OPENAI,
        help='LiteLLM: Odoo sends the conversation to /v1/chat/completions and posts the answer; it never sends '
             'tools, so the connection is chat-only. n8n: Odoo sends the conversation to a webhook; what the '
             'workflow does (data lookups, tools, memory) is up to the workflow.',
    )
    endpoint_url = fields.Char(
        string='Endpoint URL',
        help='LiteLLM: the base URL ending in /v1 (…/chat/completions is added). n8n: the full production '
             'webhook URL (a Chat Trigger: …/webhook/<id>/chat). Must be https, except localhost and '
             '*.svc.cluster.local.',
    )
    api_key = fields.Char(
        string='API Key', compute='_compute_api_key', inverse='_inverse_api_key', store=False, copy=False,
        help='LiteLLM (required): sent as "Authorization: Bearer" (a LiteLLM virtual key: rate limits are set '
             'there). n8n (optional): the signing secret of the X-Woow-Signature HMAC (never sent); without '
             'one requests are not signed, e.g. for a public n8n Chat Trigger. Leave empty to keep the current '
             'key; the saved key is never shown again. Changing the endpoint host or the backend type removes '
             'the saved key, so it is never sent to another server: enter it again.',
    )
    api_key_set = fields.Boolean(string='API Key Configured', compute='_compute_api_key')
    model = fields.Char(string='Model',
                        help='LiteLLM only (required): the LiteLLM model name, which picks the deployment. A '
                             'LiteLLM connection without one is not ready and fails Test Connection.')
    timeout_seconds = fields.Integer(
        string='Timeout (seconds)', default=LITELLM_DEFAULT_TIMEOUT,
        help='Total time allowed for one answer, retries included (10-300 seconds). New connections: 45 '
             'seconds for LiteLLM, 120 seconds for n8n. Live Chat replies are not streamed: the visitor gets '
             'the whole answer, or the error message when the time is up. A timed-out call is never retried.')
    max_retries = fields.Integer(
        string='Retries', default=DEFAULT_MAX_RETRIES,
        help='Retries (0-2) when the connection could not be opened or the backend answered HTTP 429 or 503, '
             'after 3 and 10 seconds and only within the timeout. Read timeouts and other errors are never '
             'retried: the backend may already be working on the answer.')
    document_mode = fields.Selection(
        [('file', 'Send the files to the model'), ('link', 'Download links only')],
        string='Documents Users Send', default='file', required=True,
        help='LiteLLM only: how the documents visitors send (PDF, docx, xlsx, xls, csv, txt, md, json) reach '
             'the model. "Send the files": each document of at most 4 MB is sent as it is, as a file part '
             '(LiteLLM reads PDF, Word and Excel files); Odoo converts nothing. "Download links only": a signed '
             '1-hour link instead, for a server that refuses file parts. Images are always sent inline. n8n '
             'always gets download links.')
    log_payload = fields.Boolean(
        string='Log Request Content',
        help='Debugging only: store the request and response content in the call logs. '
             'They contain private conversations. Turning it off erases stored content.',
    )
    customer_safe = fields.Boolean(
        string='Safe for Customers', copy=False,
        help='LiteLLM connections are chat-only (Odoo sends the conversation, never tools): always safe. n8n: '
             'tick it only after checking that the workflow looks up no company data, calls no tools and keeps '
             'no memory across conversations; it is cleared when the endpoint URL or the backend type changes. '
             'Live Chat visitors are mostly anonymous customers: a channel whose AI assistant uses a connection '
             'that is not safe shows a warning.')
    legacy_migrated = fields.Boolean(
        string='Created From 1.x Channel Settings', readonly=True, copy=False,
        help='Created by the 18.0.2.0.0 upgrade from the backend settings of Live Chat channels.')
    readiness = fields.Selection(
        [(READINESS_OK, 'Ready'), (READINESS_BLOCKED, 'Blocked')], string='Readiness',
        compute='_compute_readiness', search='_search_readiness',
        help='Ready: the connection can be used. Blocked: see the details.')
    readiness_reason = fields.Text(string='Readiness Details', compute='_compute_readiness')

    assistant_ids = fields.One2many('im_livechat.ai.assistant', 'connection_id', string='AI Assistants',
                                    context={'active_test': False}, readonly=True)
    assistant_count = fields.Integer(compute='_compute_assistant_count')
    call_log_count = fields.Integer(compute='_compute_call_log_count')

    def init(self):
        # SQL-only column: never an ORM field, so never returned by any read.
        self.env.cr.execute("ALTER TABLE im_livechat_ai_connection ADD COLUMN IF NOT EXISTS api_key_secret VARCHAR")

    # --- API key (write-only) ---

    def _compute_api_key(self):
        keys = self._get_api_keys()
        for connection in self:
            connection.api_key = False
            connection.api_key_set = bool(keys.get(connection._origin.id or connection.id))

    def _inverse_api_key(self):
        for connection in self:
            new_key = (connection.api_key or '').strip()
            if new_key:
                connection._check_admin()
                connection._set_api_key(new_key)
        # The typed key must not be echoed back by web_save's read.
        self.invalidate_recordset(['api_key', 'api_key_set', 'readiness', 'readiness_reason'])

    def _set_api_key(self, key):
        """Store (or remove, ``key`` empty) the key in the SQL-only column. The
        18.0.1.x copy of the key on the Live Chat channels of this connection
        (``im_livechat_channel.ai_api_token_secret``, kept for a rollback) is
        removed too, so a replaced or removed key never comes back."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute("UPDATE im_livechat_ai_connection SET api_key_secret = %s WHERE id = %s",
                            (key or None, self.id))
        self._drop_legacy_channel_keys()
        self.invalidate_recordset(['api_key', 'api_key_set', 'readiness', 'readiness_reason'])
        _logger.info("Live chat AI connection %s: API key %s by user %s", self.id,
                     'updated' if key else 'removed', self.env.uid)

    def _drop_legacy_channel_keys(self, channel_ids=None):
        """Remove the 18.0.1.x key copies of the channels whose AI assistant uses
        these connections (or of ``channel_ids``), when that column still exists."""
        cr = self.env.cr
        if not sql.column_exists(cr, 'im_livechat_channel', LEGACY_TOKEN_COLUMN):
            return 0
        if channel_ids is not None:
            ids = [i for i in channel_ids if isinstance(i, int)]
            if not ids:
                return 0
            cr.execute("UPDATE im_livechat_channel SET ai_api_token_secret = NULL "
                       "WHERE id IN %s AND ai_api_token_secret IS NOT NULL", (tuple(ids),))
            return cr.rowcount
        ids = [i for i in self.ids if isinstance(i, int)]
        if not ids:
            return 0
        self.env['im_livechat.ai.assistant'].flush_model(['connection_id'])
        self.env['im_livechat.channel'].flush_model(['ai_assistant_id'])
        cr.execute("""
            UPDATE im_livechat_channel c SET ai_api_token_secret = NULL
              FROM im_livechat_ai_assistant a
             WHERE c.ai_assistant_id = a.id AND a.connection_id IN %s AND c.ai_api_token_secret IS NOT NULL
        """, (tuple(ids),))
        return cr.rowcount

    def _get_api_keys(self):
        """{connection id: stored key} read with SQL; callers never expose the values."""
        ids = [i for i in {record._origin.id or record.id for record in self} if isinstance(i, int)]
        if not ids:
            return {}
        self.env.cr.execute("SELECT id, api_key_secret FROM im_livechat_ai_connection WHERE id IN %s",
                            (tuple(ids),))
        return dict(self.env.cr.fetchall())

    def _get_api_key(self):
        """The key as it is sent (surrounding whitespace removed, as 18.0.1.x did)."""
        self.ensure_one()
        return (self._get_api_keys().get(self._origin.id or self.id) or '').strip()

    def action_clear_api_key(self):
        # Public action_* methods are reachable over XML-RPC/JSON-RPC: return
        # True, never None (XML-RPC cannot marshal None).
        self._check_admin()
        guarded = self._in_use_snapshot()
        for connection in self:
            connection._set_api_key(False)
        self._check_in_use(guarded)
        return True

    def _check_admin(self):
        # (a request without a user, uid None: nobody)
        if not self.env.su and not (self.env.uid and self.env.user.has_group(SYSTEM_GROUP)):
            raise AccessError(self.env._('Only system administrators can change AI connections.'))

    # --- Readiness ---

    def _readiness_reasons(self, has_key=None):
        """Fixed sentences why this connection cannot be used (empty: ready).
        Never a URL, a key or a name."""
        self.ensure_one()
        _ = self.env._
        if has_key is None:
            has_key = bool(self._get_api_key()) or bool((self.api_key or '').strip())
        url = (self.endpoint_url or '').strip()
        reasons = []
        if not self.active:
            reasons.append(_('The connection is archived.'))
        if not url:
            reasons.append(_('Set the endpoint URL.'))
        elif not endpoint_url_allowed(url):
            reasons.append(_('The endpoint URL is not allowed: it must use https (plain http is only allowed '
                             'for localhost and *.svc.cluster.local).'))
        if self.backend_type == BACKEND_OPENAI and not has_key:
            reasons.append(_('Set the API key: LiteLLM connections need one.'))
        if self.backend_type == BACKEND_OPENAI and not (self.model or '').strip():
            reasons.append(_('Set the model: LiteLLM picks the deployment by its name.'))
        return reasons

    @api.depends('active', 'backend_type', 'endpoint_url', 'api_key', 'model')
    def _compute_readiness(self):
        keys = self._get_api_keys()
        for connection in self:
            has_key = bool((keys.get(connection._origin.id or connection.id) or '').strip()) \
                or bool((connection.api_key or '').strip())
            reasons = connection._readiness_reasons(has_key=has_key)
            connection.readiness = READINESS_BLOCKED if reasons else READINESS_OK
            connection.readiness_reason = '\n'.join(reasons) if reasons else self.env._('Ready to use.')

    def _search_readiness(self, operator, value):
        if operator not in ('=', '!=') or value not in (READINESS_OK, READINESS_BLOCKED):
            raise UserError(self.env._('Readiness can only be searched as equal to "ok" or "blocked".'))
        # few records: the rule is the one of _readiness_reasons (also the URL check)
        connections = self.sudo().with_context(active_test=False).search([])
        ready = connections.filtered(lambda c: c.readiness == READINESS_OK)
        wants_ready = (operator == '=') == (value == READINESS_OK)
        return [('id', 'in' if wants_ready else 'not in', ready.ids)]

    # --- Runtime values (read through sudo; callers never expose the key) ---

    def _runtime_url(self):
        """The URL as the runtime calls it (LiteLLM: base URL, no trailing slash,
        no /chat/completions)."""
        self.ensure_one()
        url = (self.sudo().endpoint_url or '').strip()
        return normalize_openai_url(url) if self.sudo().backend_type == BACKEND_OPENAI else url

    def _timeout_value(self):
        """Seconds one answer may take (10-300), retries included."""
        self.ensure_one()
        connection = self.sudo()
        seconds = connection.timeout_seconds or 0
        if seconds <= 0:
            return default_timeout(connection.backend_type)
        return max(MIN_TIMEOUT, min(seconds, MAX_TIMEOUT))

    def _max_retries_value(self):
        self.ensure_one()
        return max(0, min(self.sudo().max_retries or 0, MAX_MAX_RETRIES))

    # --- Constraints ---

    @api.constrains('endpoint_url')
    def _check_endpoint_url(self):
        for connection in self.sudo():
            url = (connection.endpoint_url or '').strip()
            if url and not endpoint_url_allowed(url):
                raise ValidationError(self.env._(
                    'The endpoint URL must use https (plain http is only allowed for localhost '
                    'and *.svc.cluster.local).'))

    @api.constrains('timeout_seconds', 'max_retries')
    def _check_limits(self):
        for connection in self.sudo():
            if not MIN_TIMEOUT <= connection.timeout_seconds <= MAX_TIMEOUT:
                raise ValidationError(self.env._('The timeout must be between 10 and 300 seconds.'))
            if not 0 <= connection.max_retries <= MAX_MAX_RETRIES:
                raise ValidationError(self.env._('Retries must be between 0 and 2.'))

    @api.constrains('backend_type', 'customer_safe')
    def _check_customer_safe(self):
        for connection in self.sudo():
            if connection.backend_type == BACKEND_OPENAI and not connection.customer_safe:
                raise ValidationError(self.env._(
                    'LiteLLM connections are chat-only: they are always safe for customers.'))

    # --- The "in use" guard: an AI-enabled channel always has a complete connection ---

    def _in_use_channels(self):
        """AI-enabled Live Chat channels whose AI assistant uses these connections (sudo)."""
        ids = [i for i in self.ids if isinstance(i, int)]
        if not ids:
            return self.env['im_livechat.channel'].sudo()
        return self.env['im_livechat.channel'].sudo().search([
            ('ai_enabled', '=', True), ('ai_assistant_id.connection_id', 'in', ids)])

    def _in_use_snapshot(self):
        """{connection id: True} for the connections that are ready now and used by
        an AI-enabled channel (checked again after the change)."""
        if self.env.context.get(NO_IN_USE_CHECK):
            return {}
        channels = self._in_use_channels()
        used = set(channels.ai_assistant_id.connection_id.ids)
        return {connection.id: True for connection in self.sudo()
                if connection.id in used and connection.readiness == READINESS_OK}

    def _check_in_use(self, snapshot):
        """Refuse a change that archived, or made not ready, a connection an
        AI-enabled channel answers with (18.0.1.x refused saving such a channel)."""
        if not snapshot:
            return
        connections = self.sudo().browse(list(snapshot))
        connections.invalidate_recordset(['readiness', 'readiness_reason'])
        broken = connections.filtered(lambda c: c.readiness != READINESS_OK)
        if not broken:
            return
        channel = broken._in_use_channels()[:1]
        raise UserError(self.env._(
            'The Live Chat channel "%(name)s" answers visitors with this AI connection. Turn the AI of that '
            'channel off first, or choose another AI assistant for it.', name=channel.name or '?'))

    # --- Writes ---

    def _target_changes(self, vals, keep_when_empty=False):
        """True when `vals` points this connection at another server (other
        scheme / host / port, endpoint removed) or changes the backend type.
        ``keep_when_empty``: a first endpoint (none before) is not a change."""
        self.ensure_one()
        connection = self.sudo()
        if 'backend_type' in vals and vals['backend_type'] != connection.backend_type:
            return True
        if 'endpoint_url' in vals:
            old = endpoint_origin(connection.endpoint_url)
            if not old and keep_when_empty:
                return False
            return endpoint_origin(vals['endpoint_url']) != old
        return False

    def _workflow_changes(self, vals):
        """True when `vals` changes the backend type or the endpoint URL itself
        (another n8n workflow may sit behind another path of the same host)."""
        self.ensure_one()
        connection = self.sudo()
        if 'backend_type' in vals and vals['backend_type'] != connection.backend_type:
            return True
        return 'endpoint_url' in vals \
            and (vals['endpoint_url'] or '').strip() != (connection.endpoint_url or '').strip()

    @api.onchange('backend_type')
    def _onchange_backend_type_timeout(self):
        # the form's default_get already filled in the LiteLLM default: swap it while
        # it is still the other backend's default
        for connection in self:
            if connection.backend_type == BACKEND_N8N and connection.timeout_seconds == LITELLM_DEFAULT_TIMEOUT:
                connection.timeout_seconds = N8N_DEFAULT_TIMEOUT
            elif connection.backend_type == BACKEND_OPENAI and connection.timeout_seconds == N8N_DEFAULT_TIMEOUT:
                connection.timeout_seconds = LITELLM_DEFAULT_TIMEOUT

    @api.onchange('backend_type', 'endpoint_url')
    def _onchange_customer_safe(self):
        # the form shows what saving does (write())
        for connection in self:
            origin = connection._origin
            if connection.backend_type == BACKEND_OPENAI:
                connection.customer_safe = True
            elif not origin or origin.backend_type != connection.backend_type \
                    or (origin.endpoint_url or '').strip() != (connection.endpoint_url or '').strip():
                connection.customer_safe = False

    @staticmethod
    def _clean_url_vals(vals):
        if 'endpoint_url' in vals:
            vals['endpoint_url'] = (vals['endpoint_url'] or '').strip() or False
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._clean_url_vals(dict(vals)) for vals in vals_list]
        for vals in vals_list:
            backend_type = vals.get('backend_type') or BACKEND_OPENAI
            if not vals.get('timeout_seconds'):
                vals['timeout_seconds'] = default_timeout(backend_type)
            if backend_type == BACKEND_OPENAI:
                vals['customer_safe'] = True
        return super().create(vals_list)

    def write(self, vals):
        vals = self._clean_url_vals(dict(vals))
        if vals.get('backend_type') == BACKEND_OPENAI:
            vals['customer_safe'] = True
        guarded = {}
        if {'active', 'backend_type', 'endpoint_url', 'model', 'api_key'} & set(vals):
            guarded = self._in_use_snapshot()
        drop_key = unsafe = self.browse()
        if {'endpoint_url', 'backend_type'} & set(vals):
            keys = self._get_api_keys()
            if not (vals.get('api_key') or '').strip():
                # a key set while the URL was empty survives filling the URL in
                drop_key = self.filtered(lambda c: keys.get(c.id) and c._target_changes(vals, keep_when_empty=True))
            if 'customer_safe' not in vals:
                # an administrator's "safe for customers" was about one n8n workflow
                unsafe = self.filtered(lambda c: c.customer_safe and c._workflow_changes(vals))
        erase = self.browse()
        if 'log_payload' in vals and not vals['log_payload']:
            erase = self.filtered('log_payload')
        res = super().write(vals)
        for connection in drop_key:
            connection._set_api_key(False)
            _logger.warning("Live chat AI connection %s: saved API key removed because the endpoint host or the "
                            "backend type changed (user %s)", connection.id, self.env.uid)
        unsafe = unsafe.filtered(lambda c: c.backend_type == BACKEND_N8N)
        if unsafe:
            unsafe.write({'customer_safe': False})
            _logger.warning("Live chat AI connection(s) %s: no longer safe for customers (the n8n workflow URL or "
                            "the backend type changed; user %s)", unsafe.ids, self.env.uid)
        if erase:
            # Turning content logging off also erases what was captured.
            self.env['llm.api.log'].sudo()._erase_payloads(connection_ids=erase.ids)
        self._check_in_use(guarded)
        return res

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for connection, vals in zip(self, vals_list):
            vals['name'] = self.env._('%s (copy)', connection.name)
            vals['legacy_migrated'] = False
            if vals.get('backend_type', connection.backend_type) == BACKEND_OPENAI:
                vals['customer_safe'] = True
        return vals_list

    # --- Test connection (system administrators) ---

    def action_test_connection(self):
        self.ensure_one()
        return self._test_connection()

    def _test_connection(self, assistant=None, channel=None):
        """Check the backend without running the AI.

        LiteLLM: ``GET {endpoint}/models`` with the key (a chat completion would
        run the model and use tokens); the configured model must be in the
        answer's ``data[].id`` list, otherwise a warning is shown. n8n: the
        ping ``{"event": "ping", "contact": … (from a channel), "assistant": …
        (when known), "connection": …, "action": "loadPreviousSession",
        "sessionId": …}``, signed only when a signing secret is set (a Chat
        Trigger answers loadPreviousSession without running the agent; any HTTP
        200 is a success). Redirects are never followed; only the HTTP status
        and a short sentence are shown, never the upstream answer."""
        self.ensure_one()
        self._check_admin()
        connection = self.sudo()
        _ = self.env._
        api_key = connection._get_api_key()
        endpoint = (connection.endpoint_url or '').strip()
        model = (connection.model or '').strip()
        status, kind = None, 'danger'
        if not endpoint and connection.backend_type == BACKEND_N8N:
            message = _('Set the endpoint URL first.')
        elif not endpoint or (connection.backend_type == BACKEND_OPENAI and not api_key):
            message = _('Set the endpoint URL and the API key first.')
        elif not endpoint_url_allowed(endpoint):
            message = _('The endpoint URL must use https (plain http is only allowed for localhost '
                        'and *.svc.cluster.local).')
        elif connection.backend_type == BACKEND_OPENAI and not model:
            message = _('Set the LiteLLM model name first.')
        else:
            models_list = None
            try:
                if connection.backend_type == BACKEND_OPENAI:
                    response = requests.get(normalize_openai_url(endpoint) + '/models',
                                            headers={'Authorization': 'Bearer %s' % api_key},
                                            timeout=(CONNECT_TIMEOUT, TEST_TIMEOUT), allow_redirects=False)
                    if response.status_code == 200:
                        try:
                            data = response.json()
                            models_list = [item.get('id') for item in data.get('data')
                                           if isinstance(item, dict)]
                        except Exception:  # noqa: BLE001 - an unparsable list is a plain success
                            models_list = None
                else:
                    ping = {'event': 'ping'}
                    if channel:
                        ping['contact'] = {'id': channel.id, 'name': channel.name or ''}
                    if assistant:
                        ping['assistant'] = {'id': assistant.id, 'name': assistant.name or ''}
                    ping['connection'] = {'id': connection.id, 'name': connection.name or ''}
                    ping['action'] = 'loadPreviousSession'
                    if channel:
                        ping['sessionId'] = 'woow-ai-ping-%s' % channel.id
                    elif assistant:
                        ping['sessionId'] = 'woow-ai-ping-assistant-%s' % assistant.id
                    else:
                        ping['sessionId'] = 'woow-ai-ping-connection-%s' % connection.id
                    body = json_dumps(ping).encode()
                    headers = {'Content-Type': 'application/json'}
                    if api_key:
                        headers.update(n8n_signature_headers(api_key, body))
                    response = requests.post(endpoint, data=body, headers=headers,
                                             timeout=(CONNECT_TIMEOUT, TEST_TIMEOUT), allow_redirects=False)
                status = response.status_code
                response.close()
            except requests.Timeout:
                message = _('The backend did not answer within %s seconds.', TEST_TIMEOUT)
            except requests.RequestException:
                message = _('Could not connect to the backend. Check the URL and the network policy.')
            if status is not None:
                if status == 200:
                    kind = 'success'
                    message = _('Connection OK (HTTP %s).', status)
                    if models_list is not None and model not in models_list:
                        kind = 'warning'
                        message = _('Connection OK (HTTP 200), but the model "%(model)s" is not in the list of '
                                    'models this key can use. Check the model name.', model=model)
                elif status in (401, 403):
                    message = _('The backend refused the key or the signature (HTTP %s).', status)
                elif status == 404:
                    message = _('Endpoint not found (HTTP %s). Check the URL.', status)
                elif 300 <= status < 400:
                    message = _('The backend answered with a redirect (HTTP %s): enter the final URL, '
                                'redirects are not followed.', status)
                else:
                    message = _('The backend answered HTTP %s.', status)
        _logger.info("Live chat AI connection %s: connection test by user %s: HTTP %s", connection.id,
                     self.env.uid, status)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Test Connection'),
                'message': message,
                'type': kind,
                'sticky': kind != 'success',
            },
        }

    # --- Assistants and call logs ---

    def _compute_assistant_count(self):
        data = self.env['im_livechat.ai.assistant'].sudo().with_context(active_test=False)._read_group(
            [('connection_id', 'in', self.ids)], ['connection_id'], ['__count'])
        counts = {connection.id: count for connection, count in data}
        for connection in self:
            connection.assistant_count = counts.get(connection.id, 0)

    def _compute_call_log_count(self):
        data = self.env['llm.api.log'].sudo()._read_group(
            [('connection_id', 'in', self.ids)], ['connection_id'], ['__count'])
        counts = {connection.id: count for connection, count in data}
        for connection in self:
            connection.call_log_count = counts.get(connection.id, 0)

    def action_view_assistants(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('im_livechat_ai.action_im_livechat_ai_assistant')
        action['domain'] = [('connection_id', '=', self.id)]
        action['context'] = {'default_connection_id': self.id, 'active_test': False}
        return action

    def action_view_call_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('im_livechat_ai.llm_api_log_action')
        action['domain'] = [('connection_id', '=', self.id)]
        action['context'] = {}
        return action
