# -*- coding: utf-8 -*-
"""Live Chat channel: the AI assistant that answers its visitors.

18.0.2.0.0: the AI is configured in two layers, aligned with Discuss AI and
Project AI. A system administrator sets up 「AI 連線」 (``im_livechat.ai.connection``:
LiteLLM or n8n, URL, key, model, timeout, retries, documents, content
logging) and 「AI 助理」 (``im_livechat.ai.assistant``: the name visitors see,
avatar, bot partner, prompt, error message, history length) and ticks
「可用在 Live Chat」. A Live Chat manager picks one of those assistants on the
channel's 「AI 助理」 tab and turns the AI on; the channel keeps the switch,
the welcome and handoff texts, the pause and wait minutes and the keywords.
Odoo records, forwards and manages the conversation; the backend does
everything else (persona, tools, product data, moderation, permissions).

Wire format (docs/PROTOCOL.md), aligned with Discuss AI and Project AI:

- LiteLLM (``openai``): ``POST {url}/chat/completions`` with
  ``Authorization: Bearer <key>``, body ``model``, ``messages`` (start and
  end with a user turn), ``user`` (session id) and ``metadata`` (strings, no
  email). No tools, no temperature, no max_tokens; not streamed.
- n8n: ``POST {url}`` with ``X-Woow-Timestamp`` and ``X-Woow-Signature:
  sha256=HMAC(secret, "<ts>.<raw body>")`` when a signing secret is set (never
  sent). The body carries ``assistant``, ``surface`` and the Live Chat
  channel as ``contact``, plus the n8n Chat Trigger fields ``action`` /
  ``sessionId`` / ``chatInput``. The answer is ``reply`` / ``replies`` /
  ``skip``, or the AI Agent's ``output``.

Visitor attachments go to the backend as they are, no text extraction:
LiteLLM gets images as ``image_url`` data URIs and, in the connection's 'file'
document mode, documents as ``file`` parts; everything else, and n8n always,
gets signed download URLs (``/im_livechat_ai/attachment/<id>/<expires>.<hmac>``,
valid 1 hour). See discuss_channel.py for the limits.
"""

import bisect
import json
import logging
import re
import time
from datetime import timedelta
from urllib.parse import urlsplit

import requests

from odoo import api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.tools import SQL, clean_context

from ..tools.backend import (  # noqa: F401 - endpoint_url_allowed and n8n_signature_headers are re-exported
    BACKEND_N8N,
    BACKEND_OPENAI,
    endpoint_url_allowed,
    exception_frames,
    json_dumps,
    one_line_name,
    n8n_signature_headers,
    request_not_sent,
)
from ..tools.markdown import markdown_to_html
from .ai_assistant import DEFAULT_BOT_XMLID, bot_avatar
from .ai_connection import (
    CONNECT_TIMEOUT,
    LITELLM_DEFAULT_TIMEOUT,
    MAX_MAX_RETRIES,
    MAX_TIMEOUT,
    N8N_DEFAULT_TIMEOUT,
    TEST_TIMEOUT,
)
from .discuss_channel import redact_base64, strip_handoff_marker

_logger = logging.getLogger(__name__)

SYSTEM_GROUP = 'base.group_system'
# Kept for one version on upgraded databases (rollback copy of the 18.0.1.x key): the column
# is not an ORM field any more; neutralize.sql and the uninstall hook clear / drop it.
TOKEN_COLUMN = 'ai_api_token_secret'

# Timeouts (seconds, one answer, retries included): see ai_connection.py.
LLM_REQUEST_TIMEOUT = LITELLM_DEFAULT_TIMEOUT
N8N_REQUEST_TIMEOUT = N8N_DEFAULT_TIMEOUT
MAX_REQUEST_TIMEOUT = MAX_TIMEOUT
N8N_TEST_TIMEOUT = TEST_TIMEOUT
LLM_MAX_RETRIES = MAX_MAX_RETRIES  # retries after the first call
LLM_RETRY_BACKOFF = (3, 10)  # seconds before retry 1 and retry 2
RETRY_STATUSES = (429, 503)  # the only HTTP answers that are retried (both backends)
N8N_RETRY_STATUSES = RETRY_STATUSES
MAX_N8N_REPLIES = 10
MAX_HANDOFF_REASON_LENGTH = 300  # characters of n8n "handoff_reason" kept
METADATA_MAX_KEYS = 16
METADATA_MAX_VALUE = 512
MAX_LOG_ERROR_LENGTH = 300
MAX_LOG_PAYLOAD = 200000  # characters
# The server warning about a channel whose AI cannot answer: at most once per
# channel and process in this many seconds (a flood of visitor messages must
# not flood the log).
BLOCKED_WARNING_SECONDS = 600
# Operator alerts of a channel (cannot answer, too many unfinished replies, a
# kind of failure): at most one per channel (and kind) in this window.
ALERT_WINDOW_MINUTES = 10
# The two cannot-answer alerts of a channel, each with its own period flag and
# time (one does not hide the other): {kind: (flag field, time field)}
ALERT_KINDS = {
    'blocked': ('ai_blocked_alert_sent', 'ai_blocked_alert_at'),  # the AI is on but cannot answer
    'full': ('ai_full_alert_sent', 'ai_full_alert_at'),  # the caps of unfinished replies refuse messages
}
# Technical fields of a channel that only system administrators may set
ALERT_FIELDS = tuple(name for names in ALERT_KINDS.values() for name in names)

# --- Visitor-facing defaults (English source, translated; stored values are kept) ---
DEFAULT_ERROR_MESSAGE = (
    'Sorry, the AI assistant cannot answer right now. Please try again later, or leave your contact details '
    'and our team will get back to you as soon as possible.'
)
# The 18.0.1.0.x default; the 18.0.1.1.0 post-migration replaced it.
LEGACY_ERROR_MESSAGE = (
    'Sorry, the AI assistant is temporarily unavailable. '
    'Please try again later or leave your contact information.'
)
# Handoff keywords are an optional pre-filter (owner decision 40): empty by
# default, the backend decides the handoff ([[handoff]] / n8n "handoff").
# The 18.0.1.1.0 default list, cleared by the 18.0.1.2.0 migration where a
# channel still has it; also the example in the help text.
HANDOFF_KEYWORDS_1_1_0 = (
    '找真人,真人客服,轉真人,轉人工,人工客服,找客服,聯絡客服,我要客服,'
    'human agent,talk to a human,real person,live agent'
)
# The earlier default; the 18.0.1.1.0 post-migration replaced it.
LEGACY_HANDOFF_KEYWORDS = '真人,客服,人工,真人客服,human,agent,operator'
DEFAULT_HANDOFF_MESSAGE = 'We have notified our team. Please wait a moment.'
DEFAULT_HANDOFF_PAUSE_MINUTES = 30  # after an operator's reply
DEFAULT_HANDOFF_WAIT_MINUTES = 10  # after a visitor's handoff request, then the AI resumes
MAX_HANDOFF_PAUSE_MINUTES = 1440
# One-time bot reply to the first visitor message during a handoff wait;
# %(minutes)s is replaced with the wait minutes.
DEFAULT_PAUSE_NOTICE = (
    'We have notified our team. Please wait a moment. If nobody can answer, the AI assistant will continue '
    'in %(minutes)s minutes.'
)
# Welcome message of AI-enabled channels (AI disclosure).
DEFAULT_WELCOME_MESSAGE = (
    'Hello, I am the AI customer service assistant. How can I help you? To talk to a person, type '
    '"talk to a human".'
)
DEFAULT_BOT_NAME = 'AI Customer Service Assistant'
LEGACY_BOT_NAME = 'AI Assistant'  # the 18.0.1.0.x default, renamed by the 18.0.1.1.0 migration
# Only Live Chat managers configure the AI of a channel; the ai_* fields are
# neither readable nor writable for operators, employees, portal or public.
AI_CONFIG_GROUP = 'im_livechat.im_livechat_group_manager'

STATE_OFF = 'off'
STATE_OK = 'ok'
STATE_BLOCKED = 'blocked'

# Keywords made only of these characters (English words or phrases) must
# match as whole words, case-insensitive, with any whitespace between the
# words ("live agent" does not match "olive agents"); others (e.g. Chinese)
# match anywhere in the text.
_WORD_CHARS_RE = re.compile(r"^[0-9a-z_' \-]+$")
_PAUSE_NOTICE_PLACEHOLDER_RE = re.compile(r'%\(minutes\)[sd]')

# Reasoning blocks and special tokens some models leave in the answer. _THINK_RE is the rule; it is applied by
# _strip_think_pairs, in linear time (the pattern itself rescans to the end of the text for every opener that has
# no closer, which a long answer could use to hold the worker for seconds).
_THINK_RE = re.compile(r'<(think|thinking)>.*?</\1\s*>', re.DOTALL | re.IGNORECASE)
_THINK_OPENER_RE = re.compile(r'<(think|thinking)>', re.IGNORECASE)
_THINK_CLOSER_RE = re.compile(r'</(think|thinking)\s*>', re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r'<(?:think|thinking)>.*$', re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r'^.*?</(?:think|thinking)\s*>', re.DOTALL | re.IGNORECASE)
_SPECIAL_TOKEN_RE = re.compile(r'<\|[a-z0-9_]{1,40}\|>', re.IGNORECASE)

# {livechat channel id: time.monotonic() of the last "cannot answer" warning} (this process)
_BLOCKED_WARNED = {}
_FULL_WARNED = {}  # the same for the cap of unfinished reply jobs


class _NobodyAlerted(Exception):
    """The alert reached nobody: its claim is undone."""


class LLMNotConfigured(Exception):
    """The channel has no usable AI assistant or connection (URL, key or model missing, URL not allowed)."""


class LLMInvalidResponse(ValueError):
    """The backend answered 200 but not with the expected JSON shape."""


class LLMEmptyResponse(ValueError):
    """The backend answered with no visible content."""


def _think_name(match):
    """'think' or 'thinking' for a match of the tag name. By its length, not
    by lower(): the pattern ignores case with Unicode folding, so it matches
    'thınk' (dotless i) or 'thİnk', whose lower() is neither name."""
    return 'thinking' if len(match.group(1)) == 8 else 'think'


def _strip_think_pairs(text):
    """``_THINK_RE.sub('', text)`` in linear time: each opener is paired with
    the nearest closer of the same name after it, leftmost first."""
    closers = {'think': [], 'thinking': []}
    for match in _THINK_CLOSER_RE.finditer(text):
        closers[_think_name(match)].append((match.start(), match.end()))
    starts = {name: [start for start, _end in found] for name, found in closers.items()}
    parts, position = [], 0
    for match in _THINK_OPENER_RE.finditer(text):
        if match.start() < position:
            continue  # inside a block already removed
        name = _think_name(match)
        index = bisect.bisect_left(starts[name], match.end())
        if index == len(starts[name]):
            continue  # no closer of this name after it (an unclosed block is removed below)
        parts.append(text[position:match.start()])
        position = closers[name][index][1]
    parts.append(text[position:])
    return ''.join(parts)


def strip_think(text):
    """Remove reasoning blocks (<think>, <thinking>, also unclosed) and special tokens."""
    if not text:
        return ''
    text = _SPECIAL_TOKEN_RE.sub('', text)
    text = _strip_think_pairs(text)
    text = _THINK_OPEN_RE.sub('', text)
    text = _THINK_CLOSE_RE.sub('', text)
    return text.strip()


def openai_reply_text(data):
    """Reply text of a (non-streamed) chat completion."""
    if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or not data['choices']:
        raise LLMInvalidResponse("Invalid API response: no 'choices' field")
    choice = data['choices'][0] if isinstance(data['choices'][0], dict) else {}
    content = (choice.get('message') or {}).get('content')
    if isinstance(content, list):
        content = '\n'.join(
            part.get('text') or '' for part in content
            if isinstance(part, dict) and part.get('type') in ('text', 'output_text'))
    if content is not None and not isinstance(content, str):
        raise LLMInvalidResponse('Invalid API response: content is not text')
    return strip_think(content or '')


def n8n_replies(data):
    """n8n answer -> (list of reply texts, skip).

    Accepted: ``{"reply": {"text": …}}``, ``{"reply": "text"}``,
    ``{"replies": [{"text": …}, "text", …]}`` (at most 10 are used),
    ``{"skip": true}``, the n8n AI Agent / Chat Trigger answer
    ``{"output": "text"}``, and the same wrapped in an n8n item list
    ``[{…}]``. Anything else raises LLMInvalidResponse. Reply attachments
    are not supported (only the text is posted).

    With ``"handoff": true`` (n8n_handoff) the text is optional and
    ``skip`` is ignored: ``{"handoff": true}`` alone gives no text."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]  # "Respond to Webhook" with all incoming items
    if not isinstance(data, dict):
        raise LLMInvalidResponse('invalid response: not a JSON object')
    handoff = data.get('handoff') is True
    if handoff and not any(key in data for key in ('reply', 'replies', 'output')):
        return [], False
    if data.get('skip') is True and not handoff:
        return [], True
    if 'replies' in data:
        raw = data['replies']
        if not isinstance(raw, list):
            raise LLMInvalidResponse("invalid response: 'replies' is not a list")
    elif 'reply' in data:
        raw = [data['reply']]
    elif 'output' in data:
        if not isinstance(data['output'], str):
            raise LLMInvalidResponse("invalid response: 'output' is not a string")
        raw = [data['output']]
    else:
        raise LLMInvalidResponse("invalid response: no 'reply', 'replies', 'skip' or 'output'")
    texts = []
    for item in raw[:MAX_N8N_REPLIES]:
        if isinstance(item, str):
            item = {'text': item}
        if not isinstance(item, dict):
            raise LLMInvalidResponse('invalid response: reply is not an object')
        text = item.get('text') or ''
        if not isinstance(text, str):
            raise LLMInvalidResponse('invalid response: reply text is not a string')
        text = strip_think(text)
        if text:
            texts.append(text)
    if not texts and not handoff:
        raise LLMEmptyResponse('empty reply')
    return texts, False


def n8n_handoff(data):
    """(handoff, reason) of an n8n answer: ``"handoff": true`` (the JSON
    boolean) at the top level or in the first item of an item list, and the
    optional ``"handoff_reason"`` string (white space collapsed, at most 300
    characters kept)."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict) or data.get('handoff') is not True:
        return False, None
    reason = data.get('handoff_reason')
    reason = ' '.join(reason.split())[:MAX_HANDOFF_REASON_LENGTH] if isinstance(reason, str) else ''
    return True, reason or None


def review_handoff(texts, handoff=False):
    """Remove [[handoff]] markers from the reply texts (the visitor never
    sees them); a marker in any text hands over. Texts left empty are
    dropped. Returns (texts, handoff)."""
    kept = []
    for text in texts:
        text, found = strip_handoff_marker(text)
        handoff = handoff or found
        if text:
            kept.append(text)
    return kept, handoff


def usage_tokens(usage):
    """(prompt, completion, total) tokens of an OpenAI ``usage`` object."""
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt = number(usage.get('prompt_tokens')) or number(usage.get('input_tokens'))
    completion = number(usage.get('completion_tokens')) or number(usage.get('output_tokens'))
    total = number(usage.get('total_tokens')) or prompt + completion
    return prompt, completion, total


def log_status(error):
    """Call log status of a failed answer (the contract's keys)."""
    if isinstance(error, requests.HTTPError):
        return 'http_error'
    if isinstance(error, requests.Timeout):
        return 'timeout'
    if isinstance(error, (LLMInvalidResponse, LLMEmptyResponse)):
        return 'invalid_response'
    return 'error'


class ImLivechatChannel(models.Model):
    """Live Chat channel answered by an AI assistant."""
    _inherit = 'im_livechat.channel'

    def _default_ai_handoff_message(self):
        return self.env._('We have notified our team. Please wait a moment.')

    def _default_ai_pause_notice(self):
        return self.env._('We have notified our team. Please wait a moment. If nobody can answer, the AI '
                          'assistant will continue in %(minutes)s minutes.')

    def _default_ai_welcome_message(self):
        return self.env._('Hello, I am the AI customer service assistant. How can I help you? To talk to a '
                          'person, type "talk to a human".')

    # --- AI (Live Chat managers) ---
    ai_enabled = fields.Boolean(
        string='Answer With AI',
        groups=AI_CONFIG_GROUP,
        default=False,
        help='Answer the visitors of this channel with its AI assistant.',
    )
    ai_assistant_id = fields.Many2one(
        'im_livechat.ai.assistant', string='AI Assistant', groups=AI_CONFIG_GROUP,
        ondelete='restrict', index='btree_not_null', domain=[('livechat_selectable', '=', True)],
        help='The AI assistant that answers the visitors: one of the assistants a system administrator made '
             'available in Live Chat (Live Chat > Configuration > AI Assistants).',
    )
    ai_assistant_description = fields.Text(
        string='AI Assistant Description', related='ai_assistant_id.description', readonly=True,
        groups=AI_CONFIG_GROUP)
    ai_state = fields.Selection(
        [(STATE_OFF, 'Off'), (STATE_OK, 'Ready'), (STATE_BLOCKED, 'Blocked')],
        string='AI Status', compute='_compute_ai_state', groups=AI_CONFIG_GROUP,
        help='Off: the AI does not answer this channel. Ready: the AI answers the visitors. Blocked: the AI '
             'is on but cannot answer; see why below.')
    ai_state_reason = fields.Text(string='AI Status Details', compute='_compute_ai_state', groups=AI_CONFIG_GROUP)
    ai_customer_safe_warning = fields.Boolean(string='Connection Not Safe for Customers', compute='_compute_ai_state',
                                              groups=AI_CONFIG_GROUP)
    ai_handoff_pause_minutes = fields.Integer(
        string='Pause After Human Reply (minutes)',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_HANDOFF_PAUSE_MINUTES,
        help='When an operator posts in a conversation, the AI stops answering that '
             'conversation for this many minutes; every further operator message '
             'extends the pause again (1-1440).',
    )
    ai_handoff_wait_minutes = fields.Integer(
        string='Wait For Human After Handoff Request (minutes)',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_HANDOFF_WAIT_MINUTES,
        help='When the visitor asks for a human, the AI waits this many minutes for '
             'an operator. If no operator replies in that time, the AI answers the '
             'messages the visitor sent meanwhile (1-1440).',
    )
    ai_handoff_keywords = fields.Char(
        string='Human Handoff Keywords',
        groups=AI_CONFIG_GROUP,
        help='Optional, empty by default: the AI backend decides when the visitor wants '
             'a human (it ends its reply with [[handoff]], n8n answers "handoff": true). '
             'Comma-separated words or phrases checked before the backend is called: when '
             'a visitor message contains one of them, the AI sends the handoff message at '
             'once without calling the backend, waits for an operator and notifies the '
             'operators. Chinese entries match anywhere in the message; English entries '
             'match whole words or phrases only, case-insensitive. Example: 找真人,轉人工,'
             'talk to a human.',
    )
    ai_pause_notice = fields.Text(
        string='Waiting Notice',
        groups=AI_CONFIG_GROUP,
        default=_default_ai_pause_notice,
        help='Sent once to the first visitor message written while the AI waits for '
             'an operator after a handoff request. %(minutes)s is replaced with the '
             'wait minutes.',
    )
    ai_welcome_message = fields.Text(
        string='AI Welcome Message',
        groups=AI_CONFIG_GROUP,
        default=_default_ai_welcome_message,
        help='Shown as the welcome message of the chat window while AI is enabled '
             '(instead of the channel welcome message), so visitors know they are '
             'talking to an AI.',
    )
    ai_handoff_message = fields.Text(
        string='Handoff Message',
        groups=AI_CONFIG_GROUP,
        default=_default_ai_handoff_message,
        help='Sent to the visitor when they ask for a human operator',
    )
    # Technical: operators were told once that the AI of this channel cannot
    # answer; cleared when the AI answers again (one alert per blocked period).
    ai_blocked_alert_sent = fields.Boolean(
        string='Operators Told the AI Cannot Answer', copy=False, readonly=True, groups=SYSTEM_GROUP,
        help='The operators were alerted once that the AI of this channel cannot answer; cleared when it answers '
             'again.')
    ai_blocked_alert_at = fields.Datetime(
        string='Last Cannot-Answer Alert', copy=False, readonly=True, groups=SYSTEM_GROUP,
        help='When the operators were last alerted that the AI of this channel cannot answer: at most one such '
             'alert in 10 minutes.')
    ai_full_alert_sent = fields.Boolean(
        string='Operators Told the Reply Queue Is Full', copy=False, readonly=True, groups=SYSTEM_GROUP,
        help='The operators were alerted once that the caps of unfinished replies refuse visitor messages of this '
             'channel; cleared when a reply is queued again.')
    ai_full_alert_at = fields.Datetime(
        string='Last Reply-Queue-Full Alert', copy=False, readonly=True, groups=SYSTEM_GROUP,
        help='When the operators were last alerted that the caps of unfinished replies refuse visitor messages of '
             'this channel: at most one such alert in 10 minutes.')

    # --- Status (read with administrator rights; fixed sentences only) ---

    @api.depends('ai_enabled', 'ai_assistant_id')
    @api.depends_context('uid')
    def _compute_ai_state(self):
        for channel in self:
            record = channel.sudo()
            assistant = record.ai_assistant_id
            problems = channel._ai_problems()
            if not record.ai_enabled:
                channel.ai_state = STATE_OFF
            else:
                channel.ai_state = STATE_BLOCKED if problems else STATE_OK
            channel.ai_state_reason = '\n'.join(problems) if problems else False
            channel.ai_customer_safe_warning = bool(assistant) and not assistant.connection_id.customer_safe

    def _ai_assistant(self):
        """The channel's AI assistant (sudo), or an empty recordset."""
        self.ensure_one()
        return self.sudo().ai_assistant_id

    def _ai_connection_record(self):
        """The AI connection of the channel's assistant (sudo), or an empty recordset."""
        self.ensure_one()
        return self._ai_assistant().connection_id

    def _ai_problems(self):
        """Fixed sentences why the AI of this channel cannot answer now (empty:
        it can). Never a URL, a key or a connection name."""
        self.ensure_one()
        assistant = self._ai_assistant()
        if not assistant:
            return [self.env._('No AI assistant is chosen yet.')]
        return assistant._problems()

    # --- Connection (read through sudo; callers never expose the key) ---

    def _ai_connection(self):
        """(backend type, endpoint URL, key) of the channel's AI assistant.

        LiteLLM URLs are normalized: no trailing slash, no /chat/completions.
        Without an assistant or a connection: ('openai', '', '')."""
        self.ensure_one()
        connection = self._ai_connection_record()
        if not connection:
            return BACKEND_OPENAI, '', ''
        return connection.backend_type or BACKEND_OPENAI, connection._runtime_url(), connection._get_api_key()

    def _ai_connection_is_ready(self):
        """True when the channel's AI assistant can answer: the assistant is
        active and available in Live Chat, its connection is active and ready,
        and the URL is allowed."""
        self.ensure_one()
        return not self._ai_problems()

    # --- Constraints ---

    @api.constrains('ai_handoff_pause_minutes')
    def _check_ai_handoff_pause_minutes(self):
        for record in self:
            minutes = record.ai_handoff_pause_minutes
            if minutes < 1 or minutes > MAX_HANDOFF_PAUSE_MINUTES:
                raise ValidationError(self.env._('The pause after a human reply must be between 1 and 1440 '
                                                 'minutes.'))

    @api.constrains('ai_handoff_wait_minutes')
    def _check_ai_handoff_wait_minutes(self):
        for record in self:
            minutes = record.ai_handoff_wait_minutes
            if minutes < 1 or minutes > MAX_HANDOFF_PAUSE_MINUTES:
                raise ValidationError(self.env._('The wait for a human must be between 1 and 1440 minutes.'))

    @api.constrains('ai_assistant_id')
    def _check_ai_assistant(self):
        # sudo: managers cannot read whether an assistant is available in Live Chat
        for channel in self.sudo():
            assistant = channel.ai_assistant_id
            if assistant and not assistant.active:
                raise ValidationError(self.env._(
                    'This AI assistant is archived. Ask a system administrator to restore it, or choose '
                    'another AI assistant.'))
            if assistant and not assistant.livechat_available:
                raise ValidationError(self.env._(
                    'This AI assistant is not available in Live Chat. Ask a system administrator to tick '
                    '"Available in Live Chat" on it, or choose another AI assistant.'))

    # Re-checked when AI is enabled (values stored before these checks
    # existed, e.g. by an earlier version, must not reach the backend).
    _AI_RANGE_CHECKS = (
        '_check_ai_handoff_pause_minutes',
        '_check_ai_handoff_wait_minutes',
    )

    def _ai_check_all_ranges(self):
        for method in self._AI_RANGE_CHECKS:
            getattr(self, method)()

    def _ai_check_enable_requirements(self):
        """An AI-enabled channel needs an AI assistant that can answer.

        Runs as the saving user (e.g. a Live Chat manager); the assistant and
        its connection are only probed through sudo: no value reaches the
        error message."""
        for channel in self.sudo():
            if channel.ai_enabled and channel._ai_problems():
                raise UserError(self.env._(
                    'The Live Chat channel "%(name)s" has no AI assistant that can answer yet. A system '
                    'administrator creates the AI assistant and its AI connection in Live Chat > Configuration '
                    '> AI Assistants; then choose it here.', name=channel.name))

    # --- CRUD ---

    @api.model
    def _check_ai_config_access(self, vals):
        """Only Live Chat managers configure the AI of a channel. Field groups
        already stop reads and writes of the ai_* fields, but Odoo does not
        check field groups on create, so create (and write) check here."""
        if self.env.su:
            return
        names = {name for name in vals if name.startswith('ai_') and name in self._fields}
        if names and not self.env.user.has_group(AI_CONFIG_GROUP):
            raise AccessError(self.env._('Only Live Chat managers can set up the AI of a channel.'))
        if names & set(ALERT_FIELDS) and not self.env.user.has_group(SYSTEM_GROUP):
            raise AccessError(self.env._('Only Live Chat managers can set up the AI of a channel.'))

    @api.model
    def _ai_explicit_values(self):
        """Every stored ai_* field of a channel with its own default (or
        empty): create() is given them explicitly for a caller who may not set
        up the AI, so neither default_* context keys nor the caller's own
        ir.default rows can set one."""
        values = {}
        for name, field in self._fields.items():
            if name.startswith('ai_') and field.store and not field.compute and not field.related:
                values[name] = field.default(self) if field.default else False
        return values

    @api.model
    def _check_ai_manager(self):
        if not self.env.is_superuser() and not self.env.user.has_group(AI_CONFIG_GROUP):
            raise AccessError(self.env._('Only Live Chat managers can use this feature.'))

    @api.model
    def _check_system_admin(self):
        if not self.env.su and not (self.env.uid and self.env.user.has_group(SYSTEM_GROUP)):
            raise AccessError(self.env._('Only system administrators can manage AI connections and AI '
                                         'assistants.'))

    @api.model_create_multi
    def create(self, vals_list):
        # The default_<field> keys of the context become values inside super().create() (default_get),
        # where Odoo checks no field group: they count as values here.
        defaults = {key[len('default_'):]: value for key, value in self.env.context.items()
                    if key.startswith('default_ai_')}
        for vals in vals_list:
            self._check_ai_config_access({**defaults, **vals})
        if self.env.uid != SUPERUSER_ID and not self.env.user.has_group(AI_CONFIG_GROUP):
            # default_get also reads the caller's own ir.default rows (any employee may write them): a caller
            # who may not set up the AI gets every ai_* field explicitly (sudo() keeps the uid, so also there)
            explicit = self._ai_explicit_values()
            vals_list = [{**explicit, **vals} for vals in vals_list]
        channels = super().create(vals_list)
        # sudo: an operator may create a channel (AI off); the ai_* fields are manager-only.
        channels.sudo().filtered('ai_enabled')._ai_check_enable_requirements()
        return channels

    def write(self, vals):
        self._check_ai_config_access(vals)
        current = self.sudo()
        enabling = current.filtered(lambda c: not c.ai_enabled) if vals.get('ai_enabled') else current.browse()
        changed_assistant = current.browse()
        if 'ai_assistant_id' in vals:
            new = self.env['im_livechat.ai.assistant'].sudo().browse(vals['ai_assistant_id'] or [])
            changed_assistant = current.filtered(
                lambda c: c.ai_assistant_id.connection_id != new.connection_id)
        res = super().write(vals)
        if enabling:
            # Enabling AI re-validates every AI setting.
            enabling._ai_check_all_ranges()
        if {'ai_enabled', 'ai_assistant_id'} & set(vals):
            current._ai_check_enable_requirements()
        if changed_assistant:
            # the 18.0.1.x key copy of a channel belongs to its old connection (rollback)
            self.env['im_livechat.ai.connection']._drop_legacy_channel_keys(channel_ids=changed_assistant.ids)
        return res

    def copy_data(self, default=None):
        """A copy starts with AI off; it keeps an AI assistant that is still
        selectable and drops an archived one."""
        vals_list = super().copy_data(default=default)
        for channel, vals in zip(self, vals_list):
            if not (default or {}).get('ai_enabled'):
                vals['ai_enabled'] = False
            assistant = channel.sudo().ai_assistant_id
            if 'ai_assistant_id' not in (default or {}) and assistant and not (
                    assistant.active and assistant.livechat_available):
                vals['ai_assistant_id'] = False
        return vals_list

    # --- Live chat window (AI disclosure) ---

    def _get_channel_infos(self):
        """Visitors of an AI-enabled channel see the AI welcome message
        (rendered by the widget as the first message of the chat)."""
        infos = super()._get_channel_infos()
        channel = self.sudo()
        if channel.ai_enabled:
            infos['default_message'] = channel.ai_welcome_message or self.env._(DEFAULT_WELCOME_MESSAGE)
        return infos

    # --- Bot partners ---

    def _get_bot_partner(self):
        """Author of AI messages, read-only: the assistant's bot partner, or the
        module's default bot partner when the channel has none."""
        self.ensure_one()
        partner = self._ai_assistant().partner_id
        if partner:
            return partner
        return self.env.ref(DEFAULT_BOT_XMLID)

    @api.model
    def _ai_bot_partner_ids(self, channel=None):
        """Partners that are the AI itself: the bot partner of every AI
        assistant (archived ones included), every partner tagged 「AI
        機器人」 without a user (former 18.0.1.x channel bots), and the
        module's default bot. ``channel`` is kept for compatibility."""
        ids = set(self.env['im_livechat.ai.assistant'].sudo().with_context(active_test=False).search(
            [('partner_id', '!=', False)]).partner_id.ids)
        category = self.env.ref('im_livechat_ai.partner_category_ai_bot', raise_if_not_found=False)
        if category:
            ids.update(self.env['res.partner'].sudo().with_context(active_test=False).search(
                [('category_id', 'in', category.ids), ('user_ids', '=', False)]).ids)
        default_bot = self.env.ref(DEFAULT_BOT_XMLID, raise_if_not_found=False)
        if default_bot:
            ids.add(default_bot.id)
        return ids

    @api.model
    def _ai_bot_avatar(self):
        """Base64 avatar of the AI bot partners (shipped with the module)."""
        return bot_avatar()

    # --- Human handoff ---

    def _ai_handoff_keyword_list(self):
        """Keywords, lower case, inner whitespace collapsed to one space."""
        self.ensure_one()
        raw = self.sudo().ai_handoff_keywords or ''
        return [' '.join(k.lower().split()) for k in re.split(r'[,，、\n]', raw) if k.strip()]

    def _ai_is_handoff_request(self, text):
        """True when the visitor text contains one of the handoff keywords:
        Chinese (any non-Latin) entries anywhere in the text, English words
        and phrases as whole words, case-insensitive, any whitespace between
        the words."""
        self.ensure_one()
        text = (text or '').lower()
        if not text:
            return False
        for keyword in self._ai_handoff_keyword_list():
            if _WORD_CHARS_RE.match(keyword):
                phrase = r'\s+'.join(re.escape(word) for word in keyword.split(' '))
                pattern = r'(?<![0-9a-z_])' + phrase + r'(?![0-9a-z_])'
                if re.search(pattern, text):
                    return True
            elif keyword in text:
                return True
        return False

    def _ai_pause_minutes(self):
        """Pause after an operator's message (minutes)."""
        self.ensure_one()
        minutes = self.sudo().ai_handoff_pause_minutes or DEFAULT_HANDOFF_PAUSE_MINUTES
        return max(1, min(minutes, MAX_HANDOFF_PAUSE_MINUTES))

    def _ai_wait_minutes(self):
        """Wait for an operator after a visitor's handoff request (minutes)."""
        self.ensure_one()
        minutes = self.sudo().ai_handoff_wait_minutes or DEFAULT_HANDOFF_WAIT_MINUTES
        return max(1, min(minutes, MAX_HANDOFF_PAUSE_MINUTES))

    def _ai_lang_env(self, lang=None):
        """Environment translating into `lang` (an installed language, else the current one)."""
        return self.with_context(lang=self.env['im_livechat.ai.assistant']._installed_lang(lang)).env

    def _ai_handoff_message_text(self, lang=None):
        self.ensure_one()
        return self.sudo().ai_handoff_message or self._ai_lang_env(lang)._(DEFAULT_HANDOFF_MESSAGE)

    def _ai_pause_notice_text(self, lang=None):
        """The waiting notice with %(minutes)s replaced by the wait minutes.

        Plain replacement, not %-formatting: an administrator's text with
        other % signs or placeholders is sent as written and never raises.
        The bot message escapes the text when it is posted.
        """
        self.ensure_one()
        text = self.sudo().ai_pause_notice or self._ai_lang_env(lang)._(DEFAULT_PAUSE_NOTICE)
        return _PAUSE_NOTICE_PLACEHOLDER_RE.sub(str(self._ai_wait_minutes()), text)

    def _ai_error_text(self, lang=None):
        """The text the visitor gets when the AI cannot answer (the assistant's error message)."""
        self.ensure_one()
        return self._ai_assistant()._error_text(lang)

    # --- Runtime bounds (legacy or hand-edited values never reach the backend) ---

    def _ai_max_history_value(self):
        self.ensure_one()
        assistant = self._ai_assistant()
        return max(1, min(assistant.max_history or 50, 200))

    def _ai_document_mode(self):
        self.ensure_one()
        return self._ai_connection_record().document_mode or 'file'

    def _ai_request_timeout_value(self):
        """Seconds one answer may take (the connection's timeout, 10-300),
        retries included."""
        self.ensure_one()
        connection = self._ai_connection_record()
        if not connection:
            return LLM_REQUEST_TIMEOUT
        return connection._timeout_value()

    # --- Response Processing ---

    @staticmethod
    def _strip_think_tags(text):
        """Remove <think>/<thinking> reasoning blocks (also unclosed) and
        special tokens such as <|eot_id|> from a backend answer."""
        return strip_think(text)

    # --- Queueing ---

    def _trigger_ai_response(self, discuss_channel, message, kind='reply', run_after=None):
        """
        Queue an AI reply (or a human handoff notice) for a visitor message.

        Creates an im_livechat.ai.job in the current (visitor) transaction;
        the job is processed after commit by a background thread, or by the
        recovery cron if that thread is lost. When the channel's AI cannot
        answer (no usable assistant), no reply job is created: the
        conversation gets one fixed notice per blocked period and the
        operators one alert per blocked period of the channel.

        Args:
            discuss_channel (discuss.channel): The livechat session
            message (mail.message): The visitor's message
            kind (str): 'reply' (backend answer), 'handoff' (fixed notice and
                operator alert, no backend call) or 'notice' (waiting
                notice, no backend call)
            run_after (datetime|None): deferred reply: answer at this time
                (end of a handoff wait), run by the recovery cron

        Returns:
            im_livechat.ai.job: the queued job, or an empty recordset
        """
        self.ensure_one()
        Job = self.env['im_livechat.ai.job']
        channel = self.sudo()

        if not channel.ai_enabled:
            return Job

        if kind in ('handoff', 'notice'):
            return Job._create_for_message(discuss_channel, message, kind=kind)

        if not self._ai_connection_is_ready():
            self._ai_handle_blocked(discuss_channel, message)
            return Job

        job = Job._create_for_message(discuss_channel, message, run_after=run_after)
        if job:
            # answering again: a later blocked period gets its notice and its alert
            session = discuss_channel.sudo()
            if session.ai_blocked_notice_sent:
                session.ai_blocked_notice_sent = False
            for flag, _at in ALERT_KINDS.values():
                if channel[flag]:
                    channel[flag] = False
        elif not run_after:
            # the cap of unfinished reply jobs refused it (channel or database)
            self._ai_handle_full(discuss_channel, message)
        return job

    def _ai_handle_blocked(self, discuss_channel, message):
        """The channel's AI is on but cannot answer: no job, no backend call.
        The server log gets a warning (at most once per channel and process in
        BLOCKED_WARNING_SECONDS), the conversation one notification per
        blocked period (never read as history, never answered) and the
        operators one alert per blocked period of the channel, at most one in
        ALERT_WINDOW_MINUTES (see _ai_alert_once)."""
        self.ensure_one()
        now = time.monotonic()
        last = _BLOCKED_WARNED.get(self.id)
        if last is None or now - last >= BLOCKED_WARNING_SECONDS:
            _BLOCKED_WARNED[self.id] = now
            _logger.warning("Live chat channel %s: the AI is on but cannot answer (no usable AI assistant or "
                            "AI connection); no reply job queued", self.id)
        session = discuss_channel.sudo()
        if not session.ai_blocked_notice_sent:
            session._ai_post_blocked_notice(self)
        self._ai_alert_once('blocked', lambda: self.env['im_livechat.ai.job']._notify_blocked(self, session))

    def _ai_handle_full(self, discuss_channel, message):
        """The cap of unfinished reply jobs (of the channel, or of the
        database) refused the job: as for a blocked channel, the conversation
        gets one notice per period, the operators one alert per period of the
        channel and at most one in ALERT_WINDOW_MINUTES (a cap that refills at
        every freed slot ends a period with each queued reply), the server log
        a warning at most once per channel and process in
        BLOCKED_WARNING_SECONDS. The next queued reply ends the period."""
        self.ensure_one()
        now = time.monotonic()
        last = _FULL_WARNED.get(self.id)
        if last is None or now - last >= BLOCKED_WARNING_SECONDS:
            _FULL_WARNED[self.id] = now
            _logger.warning("Live chat channel %s: too many unfinished AI reply jobs; no reply job queued", self.id)
        session = discuss_channel.sudo()
        if not session.ai_blocked_notice_sent:
            session._ai_post_blocked_notice(self)
        self._ai_alert_once('full', lambda: self.env['im_livechat.ai.job']._notify_full(self, session))

    def _ai_alert_once(self, kind, notify):
        """Run ``notify`` (the operator alert of a blocked or a full channel,
        ``kind`` 'blocked' or 'full') once per period of the channel (its flag,
        cleared when a reply is queued again) and at most once in
        ALERT_WINDOW_MINUTES (its time): see _ai_claim_alert. ``notify``
        returns whether anybody was told; when nobody was, the claim is undone
        and the next refused message tries again."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                if self._ai_claim_alert(kind) and not notify():
                    raise _NobodyAlerted()
        except _NobodyAlerted:
            pass
        except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
            raise
        except Exception as e:  # noqa: BLE001 - the visitor's message stays; the next one tries again
            _logger.error("Live chat channel %s: could not alert the operators that the AI cannot answer: %s",
                          self.id, exception_frames(e))

    def _ai_claim_alert(self, kind):
        """Claim the cannot-answer alert ``kind`` of the channel with one UPDATE
        of its row: a visitor request (REPEATABLE READ) and the job threads
        (READ COMMITTED) never both alert. The row is taken FOR NO KEY UPDATE
        SKIP LOCKED: another claim or a write of the channel skips this one
        (the alert is that one's, or the next refused message's), while the
        KEY SHARE locks of inserts that reference the channel (sessions, jobs,
        call logs, usage) neither wait for it nor make it skip. Returns whether
        this caller alerts."""
        self.ensure_one()
        flag, at = ALERT_KINDS[kind]
        channel = self.sudo()
        channel.flush_recordset(list(ALERT_FIELDS))
        now = fields.Datetime.now()
        self.env.cr.execute(SQL(
            """UPDATE im_livechat_channel SET %(flag)s = TRUE, %(at)s = %(now)s
                WHERE id = (SELECT id FROM im_livechat_channel
                             WHERE id = %(id)s AND %(flag)s IS NOT TRUE AND (%(at)s IS NULL OR %(at)s <= %(since)s)
                             FOR NO KEY UPDATE SKIP LOCKED)""",
            flag=SQL.identifier(flag), at=SQL.identifier(at), now=now, id=self.id,
            since=now - timedelta(minutes=ALERT_WINDOW_MINUTES)))
        claimed = bool(self.env.cr.rowcount)
        channel.invalidate_recordset(list(ALERT_FIELDS))
        return claimed

    # --- Backend requests ---

    @api.model
    def _ai_session_id(self, discuss_channel):
        """Stable id of one conversation: ``woow-ai-<db uuid 8 hex>-<discuss.channel id>``
        (aligned with Discuss AI, so n8n memory keyed on it continues)."""
        uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid') or ''
        return 'woow-ai-%s-%s' % (uuid.replace('-', '')[:8], discuss_channel.id)

    def _ai_surface_info(self):
        """``surface`` of the n8n body (and ``surface_*`` LiteLLM metadata)."""
        self.ensure_one()
        return {'type': 'livechat', 'livechat_channel_id': self.id}

    @api.model
    def _ai_openai_metadata(self, session_id, identity, livechat_channel_name, surface=None):
        """LiteLLM ``metadata``: string keys and values (at most 16 keys of at
        most 512 characters; informational, LiteLLM does not pass it to the
        model): ``session_id``, the surface as ``surface_<key>``, the visitor
        as ``odoo_<key>`` (never the email), then ``livechat_channel``."""
        metadata = {'session_id': session_id}
        for key, value in (surface or {}).items():
            if value in (None, '') or isinstance(value, (dict, list, tuple)):
                continue
            if isinstance(value, bool):
                value = 'true' if value else 'false'
            metadata['surface_%s' % key] = str(value)[:METADATA_MAX_VALUE]
        for key in ('partner_id', 'guest_id', 'name', 'lang', 'is_portal', 'is_guest'):
            value = identity.get(key)
            if key == 'name':
                value = one_line_name(value)  # one line; a name with an '@' counts as no name (contract 3.14)
            if value in (None, '', False) and not isinstance(value, bool):
                continue
            if isinstance(value, bool):
                value = 'true' if value else 'false'
            metadata['odoo_%s' % key] = str(value)[:METADATA_MAX_VALUE]
        if livechat_channel_name:
            metadata['livechat_channel'] = livechat_channel_name[:METADATA_MAX_VALUE]
        return dict(list(metadata.items())[:METADATA_MAX_KEYS])

    def _ai_build_request(self, discuss_channel, conversation, trigger_message=None):
        """The HTTP request of one reply (reads the database; no HTTP).

        Returns:
            dict: backend, url, headers, payload (the JSON body as a dict),
                body (bytes, n8n), token (n8n signing secret, in memory only),
                timeout (seconds of the whole answer)
        Raises:
            LLMNotConfigured: no usable assistant / connection, or the URL not allowed
        """
        self.ensure_one()
        channel = self.sudo()
        assistant = channel.ai_assistant_id
        connection = assistant.connection_id
        if not assistant or not connection:
            raise LLMNotConfigured('no AI assistant or AI connection')
        backend, url, token = channel._ai_connection()
        if not url or (backend == BACKEND_OPENAI and not token):
            raise LLMNotConfigured('endpoint URL or API key missing')
        if not endpoint_url_allowed(url):
            raise LLMNotConfigured('endpoint URL not allowed (https only, http for local hosts)')
        session = discuss_channel.sudo()
        session_id = self._ai_session_id(session)
        identity = session._ai_visitor_identity()
        headers = {'Content-Type': 'application/json'}
        timeout = connection._timeout_value()
        if backend == BACKEND_N8N:
            payload = channel._ai_n8n_payload(session, conversation, session_id, identity, trigger_message)
            return {
                'backend': backend, 'url': url, 'headers': headers, 'payload': payload,
                'body': json_dumps(payload).encode(), 'token': token, 'timeout': timeout,
            }
        payload = {}
        model = (connection.model or '').strip()
        if model:
            payload['model'] = model
        payload['messages'] = session._ai_openai_messages(conversation, livechat_channel=channel)
        payload['user'] = session_id
        payload['metadata'] = self._ai_openai_metadata(session_id, identity, channel.name or '',
                                                       surface=channel._ai_surface_info())
        headers['Authorization'] = 'Bearer %s' % token
        return {
            'backend': backend, 'url': url + '/chat/completions', 'headers': headers,
            'payload': payload, 'body': json_dumps(payload).encode(), 'token': None, 'timeout': timeout,
        }

    def _ai_n8n_payload(self, session, conversation, session_id, identity, trigger_message=None):
        """n8n webhook body (docs/PROTOCOL.md): the contract keys, plus the
        fields an n8n Chat Trigger reads (``action``, ``sessionId``,
        ``chatInput``) and ``contact`` = this Live Chat channel (kept for the
        workflows written for 18.0.1.x; new workflows read ``assistant``)."""
        self.ensure_one()
        assistant = self.sudo().ai_assistant_id
        history = conversation['history']
        current = None
        if trigger_message:
            current = next((e for e in history if e['message_id'] == trigger_message.id
                            and e['kind'] == 'visitor'), None)
        if current is None:
            current = next((e for e in reversed(history) if e['kind'] == 'visitor'), None)
        # Signed download URLs of the visitor attachments (nothing is inlined).
        base_url = session._ai_attachment_base_url()
        now = time.time()

        def attachments(entry):
            return [session._ai_attachment_info(a, base_url=base_url, now=now)
                    for a in entry.get('attachments') or []]

        message = {'id': None, 'text': '', 'attachments': []}
        if current:
            message = {'id': current['message_id'], 'text': current['text'], 'attachments': attachments(current)}
        return {
            'event': 'message',
            'session_id': session_id,
            # n8n Chat Trigger ("hosted chat") aliases
            'action': 'sendMessage',
            'sessionId': session_id,
            'chatInput': message['text'],
            'assistant': {'id': assistant.id, 'name': assistant.name or ''},
            'contact': {'id': self.id, 'name': self.name or ''},
            'user': identity,
            'channel': session._ai_channel_info(),
            'surface': self._ai_surface_info(),
            'system_prompt': conversation['system_prompt'] or '',
            'message': message,
            'history': [{'role': e['role'], 'text': e['text'], 'attachments': attachments(e)}
                        for e in history if e is not current],
            'events': [{
                'time': event['time'].strftime('%Y-%m-%dT%H:%M:%SZ'),  # contract 3.6: UTC, no fractions
                'kind': event['kind'],
                'text': event['text'],
            } for event in conversation['events']],
        }

    @staticmethod
    def _ai_post(request, timeout=None):
        """One HTTP POST (no database access) of the request's compact UTF-8
        JSON bytes (both backends). n8n bodies are signed per call (fresh
        timestamp) when a signing secret is set; redirects are never followed.
        ``timeout``: (connect, read) seconds."""
        headers = dict(request['headers'])
        n8n = request['backend'] == BACKEND_N8N
        if timeout is None:
            seconds = request.get('timeout') or (N8N_REQUEST_TIMEOUT if n8n else LLM_REQUEST_TIMEOUT)
            timeout = (min(CONNECT_TIMEOUT, seconds), seconds)
        body = request['body'] if request.get('body') is not None else json_dumps(request['payload']).encode()
        if n8n and request['token']:
            headers.update(n8n_signature_headers(request['token'], body))
        return requests.post(request['url'], data=body, headers=headers, timeout=timeout, allow_redirects=False)

    @staticmethod
    def _ai_parse_response(backend, response):
        """HTTP response -> (reply texts, skip, data). Raises on any error."""
        if response.status_code != 200:
            _logger.warning("AI backend answered HTTP %s", response.status_code)
            raise requests.HTTPError('HTTP %s' % response.status_code, response=response)
        try:
            data = response.json()
        except ValueError:
            raise LLMInvalidResponse('invalid response: not JSON')
        if backend == BACKEND_N8N:
            texts, skip = n8n_replies(data)
            return texts, skip, data
        text = openai_reply_text(data)
        if not text:
            raise LLMEmptyResponse('Empty response from LLM API')
        return [text], False, data

    @staticmethod
    def _ai_should_retry(backend, error):
        """Both backends: only a request that was not sent (the connection could
        not be opened) and HTTP 429 / 503 (the backend refused before working);
        never a read timeout, a reset after the request was sent or any other
        answer, because the backend may already be working on it."""
        if isinstance(error, requests.HTTPError):
            response = error.response
            return response is not None and response.status_code in RETRY_STATUSES
        return request_not_sent(error)

    def _generate_ai_reply(self, discuss_channel, conversation, before_call=None, trigger_message=None, job=None):
        """
        Produce one AI reply: one backend request, bounded retries, one call log.

        The connection's timeout bounds the whole answer, retries included:
        each call reads at most the time left, and a call is retried (at most
        the connection's retries, after 3 s then 10 s) only when it could not
        be sent or the backend answered HTTP 429 / 503, and only while the
        time left allows the wait.

        Args:
            discuss_channel (discuss.channel): The chat session
            conversation (dict): result of discuss.channel._ai_conversation()
            before_call (callable|None): Called right before each HTTP call
                (the job commits there so no transaction stays open)
            trigger_message (mail.message|None): the visitor message being
                answered (n8n ``message``)
            job (im_livechat.ai.job|None): the job, for the call log

        Returns:
            dict: replies (list of str, [[handoff]] markers removed), reply
                (first reply or None), skip (bool, n8n), error (short text or
                None), calls (int), handoff (bool: the backend asked for a
                human), handoff_reason (str or None, n8n), usage ((prompt,
                completion) tokens), assistant / connection (the records the
                answer was asked from)
        """
        self.ensure_one()
        channel = self.sudo()
        assistant = channel.ai_assistant_id
        connection = assistant.connection_id
        result = {'replies': [], 'reply': None, 'skip': False, 'error': None, 'calls': 0,
                  'handoff': False, 'handoff_reason': None, 'usage': (0, 0),
                  'assistant': assistant, 'connection': connection}
        backend = connection.backend_type or BACKEND_OPENAI
        log_base = {
            'channel': channel, 'discuss_channel': discuss_channel, 'assistant': assistant,
            'connection': connection, 'job': job,
            'model_name': (connection.model or '').strip() if backend == BACKEND_OPENAI else False,
            'backend_type': backend,
        }
        started = time.time()
        try:
            request = channel._ai_build_request(discuss_channel, conversation, trigger_message)
        except LLMNotConfigured as e:
            result['error'] = self._short_llm_error(e)
            result['error_exc'] = e
            channel._create_api_log(status='error', error_message=type(e).__name__, duration=0, **log_base)
            return result

        retries = connection._max_retries_value()
        attempts = 0
        http_status = None
        while True:
            if before_call:
                before_call()  # after the last database read, right before the request
            if attempts == 0:
                started = time.time()
                deadline = started + request['timeout']
            attempts += 1
            result['calls'] += 1
            left = max(1.0, deadline - time.time())
            http_status = None
            try:
                response = self._ai_post(request, timeout=(min(CONNECT_TIMEOUT, left), left))
                http_status = getattr(response, 'status_code', None)
                texts, skip, data = self._ai_parse_response(backend, response)
                prompt, completion, total = usage_tokens(data.get('usage') if isinstance(data, dict) else None)
                channel._create_api_log(
                    status='ok', request_payload=request['payload'], response_payload=data,
                    prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                    duration=time.time() - started, retry_count=attempts - 1,
                    http_status=http_status, **log_base,
                )
                # Owner decision 40: the backend decides the handoff.
                handoff, reason = n8n_handoff(data) if backend == BACKEND_N8N else (False, None)
                texts, handoff = review_handoff(texts, handoff)
                if handoff:
                    skip = False  # with handoff, skip is ignored
                result.update(replies=texts, reply=texts[0] if texts else None, skip=skip,
                              handoff=handoff, handoff_reason=reason, usage=(prompt, completion))
                return result
            except Exception as e:  # noqa: BLE001 - every failure is retried or reported
                if isinstance(e, requests.HTTPError) and e.response is not None:
                    http_status = e.response.status_code
                # Full detail goes to the server log only (ids, status, type);
                # the stored log keeps the exception type name, never the
                # upstream body, a header or the key.
                last_error = self._short_llm_error(e)
                wait = LLM_RETRY_BACKOFF[min(attempts - 1, len(LLM_RETRY_BACKOFF) - 1)]
                can_retry = (attempts - 1 < retries and self._ai_should_retry(backend, e)
                             and time.time() + wait < deadline)
                # An unexpected exception (a defect, a library change) also gets its code frames, never its
                # text (contract 3.9); the expected failures are described by their short text.
                expected = isinstance(e, (requests.HTTPError, requests.Timeout, requests.ConnectionError,
                                          LLMNotConfigured, LLMInvalidResponse, LLMEmptyResponse))
                _logger.warning(
                    "AI backend call failed for channel %s (call %s, %s): %s%s",
                    channel.id, attempts, 'retrying' if can_retry else 'giving up', last_error,
                    '' if expected else '\n' + exception_frames(e),
                )
                if can_retry:
                    if before_call:
                        before_call()  # no transaction stays open during the wait
                    time.sleep(wait)
                    continue
                channel._create_api_log(
                    status=log_status(e), request_payload=request['payload'], error_message=type(e).__name__,
                    duration=time.time() - started, retry_count=attempts - 1, http_status=http_status,
                    **log_base,
                )
                result['error'] = last_error
                result['error_exc'] = e
                return result

    @api.model
    def _describe_llm_error(self, error):
        """Map an exception to (status_code or None, short operator-facing
        sentence). Never includes upstream response bodies, headers or the key."""
        _ = self.env._
        if isinstance(error, requests.HTTPError):
            status = error.response.status_code if error.response is not None else None
            messages = {
                400: _('the request or the model name is wrong'),
                401: _('the key or the signature is not valid'),
                403: _('the backend refused access'),
                404: _('the URL or the model does not exist'),
                408: _('the upstream service timed out'),
                413: _('the conversation is too long'),
                429: _('too many calls, or the quota is used up'),
            }
            if status in messages:
                return status, messages[status]
            if status and 300 <= status < 400:
                return status, _('the backend answered with a redirect (enter the final URL; redirects are '
                                 'not followed)')
            if status and status >= 500:
                return status, _('the AI backend or an upstream service is temporarily failing')
            return status, _('the backend answered with an error')
        if isinstance(error, requests.Timeout):
            return None, _('the backend did not answer in time')
        if isinstance(error, requests.ConnectionError):
            return None, _('could not connect to the AI backend')
        if isinstance(error, LLMNotConfigured):
            return None, _('the AI assistant or its AI connection is not set up')
        if isinstance(error, LLMInvalidResponse):
            return None, _('the answer has an invalid format')
        if isinstance(error, LLMEmptyResponse):
            return None, _('the AI answer is empty')
        return None, _('an unexpected error')

    @api.model
    def _short_llm_error(self, error):
        """Short error text for the job and the operator alert: ``HTTP <status>:``
        when there is one, a fixed sentence and the exception type name (no
        upstream body)."""
        status, message = self._describe_llm_error(error)
        prefix = f'HTTP {status}: ' if status else ''
        text = f'{prefix}{message} ({type(error).__name__})'
        return text[:MAX_LOG_ERROR_LENGTH]

    def _create_bot_message(self, env, discuss_channel, body, assistant=None):
        """
        Post a message to the livechat session as the AI bot.

        The text is rendered as markdown (tools/markdown.py: paragraphs,
        line breaks, **bold**, lists, tables, headings, code), every piece of
        it escaped first and the result sanitized, so an LLM answer can never
        inject HTML. Only links to the store's own hosts become clickable.

        Args:
            env (api.Environment): Environment with new cursor
            discuss_channel (discuss.channel): The chat session
            body (str): Message body text (markdown)
            assistant (im_livechat.ai.assistant|None): the assistant that wrote
                the answer (its bot partner is the author, also when a manager
                switched the channel's assistant while the backend answered);
                default: the channel's assistant

        Returns:
            mail.message: the posted message
        """
        self.ensure_one()

        bot_partner = assistant.sudo().partner_id if assistant and assistant.exists() else None
        if not bot_partner:
            bot_partner = self._get_bot_partner()
        safe_body = markdown_to_html(body, link_hosts=self._ai_link_hosts())

        return discuss_channel.with_context(mail_create_nosubscribe=True).message_post(
            body=safe_body,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=bot_partner.id,
        )

    @staticmethod
    def _url_host(url):
        try:
            return (urlsplit(url).hostname or '').lower()
        except ValueError:
            return ''

    @api.model
    def _ai_link_hosts(self):
        """Hosts AI replies may link to: web.base.url and the domains of the
        websites (against prompt-injected phishing links)."""
        hosts = set()
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        candidates = [base_url]
        if 'website' in self.env:
            candidates += self.env['website'].sudo().search([]).mapped('domain')
        for value in candidates:
            value = (value or '').strip()
            if not value:
                continue
            if '://' not in value:
                value = 'https://' + value
            host = self._url_host(value)
            if host:
                hosts.add(host)
        return frozenset(hosts)

    def _create_api_log(self, status, channel=None, discuss_channel=None, assistant=None, connection=None,
                        job=None, model_name=None, backend_type=None, request_payload=None,
                        response_payload=None, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                        duration=None, error_message=None, retry_count=0, http_status=None):
        """
        Create the call log of one answer (never fails the caller).

        Conversation content (request and response bodies) is only kept while
        the connection's "Log Request Content" is on (off by default). The key
        is never part of a payload, and the base64 data of inlined files is
        replaced by ``<N bytes base64 omitted>``. ``error_message`` is the
        exception type name only.
        """
        env = self.env
        try:
            vals = {
                'livechat_channel_id': (channel or self).id or False,
                'assistant_id': assistant.id if assistant else False,
                'connection_id': connection.id if connection else False,
                'job_id': job.id if job else False,
                'surface_type': 'livechat',
                'model': model_name or False,
                'backend_type': backend_type or False,
                'status': status,
                'http_status': http_status or False,
                'response_time': duration,
                'duration_ms': int((duration or 0) * 1000),
                'error_message': (error_message or '')[:MAX_LOG_ERROR_LENGTH] or False,
                'retry_count': retry_count,
                'prompt_tokens': prompt_tokens or 0,
                'completion_tokens': completion_tokens or 0,
                'total_tokens': total_tokens or 0,
            }
            if discuss_channel and env['discuss.channel'].browse(discuss_channel.id).exists():
                vals['discuss_channel_id'] = discuss_channel.id
            if connection and connection.sudo().log_payload:
                if request_payload:
                    vals['request_payload'] = json.dumps(redact_base64(request_payload), ensure_ascii=False,
                                                         default=str)[:MAX_LOG_PAYLOAD]
                if response_payload:
                    vals['response_payload'] = json.dumps(redact_base64(response_payload), ensure_ascii=False,
                                                          default=str)[:MAX_LOG_PAYLOAD]
            with env.cr.savepoint():
                # clean_context: no default_* key of a client context sets a field of the log
                return env['llm.api.log'].sudo().with_context(clean_context(env.context)).create(vals)
        except Exception as log_error:  # noqa: BLE001 - a log never breaks the reply
            _logger.error("Failed to create the AI call log: %s", type(log_error).__name__)
            return env['llm.api.log']

    # --- UI Action Methods ---

    def action_test_ai_connection(self):
        """Test the AI connection of this channel's assistant (system
        administrators only). See im_livechat.ai.connection._test_connection."""
        self.ensure_one()
        self._check_system_admin()
        assistant = self._ai_assistant()
        if not assistant or not assistant.connection_id:
            raise UserError(self.env._('Choose an AI assistant with an AI connection first.'))
        return assistant.connection_id._test_connection(assistant=assistant, channel=self.sudo())

    def action_view_api_logs(self):
        """Open the call logs of this channel (system administrators only)."""
        self.ensure_one()
        self._check_system_admin()
        action = self.env['ir.actions.act_window']._for_xml_id('im_livechat_ai.llm_api_log_action')
        action.update({
            'name': self.env._('Call Logs - %s', self.name),
            'domain': [('livechat_channel_id', '=', self.id)],
            'context': {'default_livechat_channel_id': self.id, 'search_default_filter_last_7_days': 1},
        })
        return action
