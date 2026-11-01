# -*- coding: utf-8 -*-
"""Live Chat channel: per-channel AI backend (OpenAI compatible or n8n webhook).

18.0.1.2.0: every channel has its own backend connection (type, endpoint
URL, access token), managed by system administrators on the channel's AI
tab. Odoo records, forwards and manages the conversation; the backend does
everything else (persona, tools, product data, moderation, permissions).

Wire format (docs: README "AI backends"), compatible with the n8n section of
woow_ai_contact docs/PROTOCOL.md:

- openai: ``POST {url}/chat/completions`` with ``Authorization: Bearer
  <token>``, body ``model`` (only when set), ``messages`` (start and end with
  a user turn), ``user`` (session id) and ``metadata`` (strings). No tools,
  no temperature, no max_tokens; not streamed.
- n8n: ``POST {url}`` with ``X-Woow-Timestamp`` and ``X-Woow-Signature:
  sha256=HMAC(token, "<ts>.<raw body>")`` when a token is set (never sent;
  without a token, e.g. a public Chat Trigger, nothing is signed). The body
  also carries the n8n Chat Trigger fields ``action`` / ``sessionId`` /
  ``chatInput``. The answer is ``reply`` / ``replies`` / ``skip``, or the
  AI Agent's ``output``.

Visitor attachments (owner decision 39) go to the backend as they are, no
text extraction: openai gets images as ``image_url`` data URIs and, in the
channel's 'file' document mode, documents as ``file`` parts (original bytes
and mimetype); everything else, and n8n always, gets signed download URLs
(``/im_livechat_ai/attachment/<id>/<expires>.<hmac>``, valid 1 hour). See
discuss_channel.py for the limits.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import timedelta, timezone
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from odoo import api, fields, models, tools, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from ..tools.markdown import markdown_to_html
from .discuss_channel import DOCUMENT_MODE_FILE, DOCUMENT_MODE_LINK, redact_base64

_logger = logging.getLogger(__name__)

# --- Backend connection (per channel, system administrators only) ---
BACKEND_OPENAI = 'openai'
BACKEND_N8N = 'n8n'
# The access token lives in this SQL-only column (not an ORM field), so no
# read of the channel ever returns it; ai_api_token is write-only.
TOKEN_COLUMN = 'ai_api_token_secret'
SYSTEM_GROUP = 'base.group_system'
# Fields only system administrators may set (read access: field groups).
AI_SYSTEM_FIELDS = frozenset({'ai_backend_type', 'ai_endpoint_url', 'ai_api_token', 'ai_log_payload'})
# Plain http:// is accepted only for these hosts (same rule as woow_ai_contact).
HTTP_ALLOWED_HOSTS = ('localhost', '127.0.0.1', '::1')
HTTP_ALLOWED_SUFFIX = '.svc.cluster.local'

DEFAULT_LOG_RETENTION_DAYS = 14

# Seconds to wait for the backend's answer, per call: the channel's
# ai_request_timeout (10-300), or these defaults when it is 0. The n8n smart
# home advisor took 112 s on a long whole-house question (live test
# 2026-11-01; Cloudflare did not cut it at 100 s), so n8n waits 120 s.
LLM_REQUEST_TIMEOUT = 45  # OpenAI compatible default
N8N_REQUEST_TIMEOUT = 120  # n8n default
MIN_REQUEST_TIMEOUT = 10
MAX_REQUEST_TIMEOUT = 300
LLM_MAX_RETRIES = 2  # retries after the first call
LLM_RETRY_BACKOFF = (3, 10)  # seconds before retry 1 and retry 2
N8N_RETRY_STATUSES = (429, 503)  # n8n: the only HTTP answers that are retried
N8N_TEST_TIMEOUT = 15  # seconds, ping of the connection test
MAX_N8N_REPLIES = 10
METADATA_MAX_VALUE = 512
MAX_LOG_ERROR_LENGTH = 300
CONNECTION_TEST_PROMPT = 'Connection test. Reply with "OK".'

# --- Visitor-facing defaults (Traditional Chinese, P2-7 / P1-2) ---
DEFAULT_ERROR_MESSAGE = (
    '抱歉，AI 客服暫時無法回覆。請稍後再試，或留下您的聯絡方式，'
    '我們的客服人員會盡快與您聯繫。'
)
# The 18.0.1.0.x default; the 18.0.1.1.0 post-migration replaces it.
LEGACY_ERROR_MESSAGE = (
    'Sorry, the AI assistant is temporarily unavailable. '
    'Please try again later or leave your contact information.'
)
# Explicit requests only (P0-1): single words such as 客服/人工/agent no
# longer hand the conversation over.
DEFAULT_HANDOFF_KEYWORDS = (
    '找真人,真人客服,轉真人,轉人工,人工客服,找客服,聯絡客服,我要客服,'
    'human agent,talk to a human,real person,live agent'
)
# The earlier default; the 18.0.1.1.0 post-migration replaces it.
LEGACY_HANDOFF_KEYWORDS = '真人,客服,人工,真人客服,human,agent,operator'
DEFAULT_HANDOFF_MESSAGE = '已幫您通知客服，請稍候。'
DEFAULT_HANDOFF_PAUSE_MINUTES = 30  # after an operator's reply
DEFAULT_HANDOFF_WAIT_MINUTES = 10  # after a visitor's handoff request, then the AI resumes
MAX_HANDOFF_PAUSE_MINUTES = 1440
# One-time bot reply to the first visitor message during a handoff wait;
# %(minutes)s is replaced with the wait minutes.
DEFAULT_PAUSE_NOTICE = (
    '已通知客服人員，請稍候。若客服暫時無法回覆，AI 助理會在 %(minutes)s 分鐘後繼續為您服務。'
)
# Welcome message of AI-enabled channels (AI disclosure, P1-7).
DEFAULT_WELCOME_MESSAGE = (
    '您好，我是 AI 客服助理，有什麼可以幫您？需要真人客服請輸入「找真人」。'
)
DEFAULT_BOT_NAME = 'AI 客服助理'
LEGACY_BOT_NAME = 'AI Assistant'  # the 18.0.1.0.x default, renamed by the 18.0.1.1.0 migration
BOT_AVATAR_PATH = 'im_livechat_ai/static/img/ai_bot.png'
MAX_AI_MAX_HISTORY = 200
DEFAULT_AI_MAX_HISTORY = 50
MIN_LOG_RETENTION_DAYS = 1
MAX_LOG_RETENTION_DAYS = 3650
# Only Live Chat managers configure the AI of a channel; the ai_* fields are
# neither readable nor writable for operators, employees, portal or public.
AI_CONFIG_GROUP = 'im_livechat.im_livechat_group_manager'
BOT_CATEGORY_XMLID = 'im_livechat_ai.partner_category_ai_bot'
# Reply job monitoring on the AI tab: a recovery cron that has not run for
# this long, or unfinished jobs this old, are reported.
MONITOR_STALE_MINUTES = 5


# Keywords made only of these characters (English words or phrases) must
# match as whole words, case-insensitive, with any whitespace between the
# words ("live agent" does not match "olive agents"); others (e.g. Chinese)
# match anywhere in the text.
_WORD_CHARS_RE = re.compile(r"^[0-9a-z_' \-]+$")

# Handoff decision (live test 2026-11-01): a keyword that is only MENTIONED
# in a question about the support service ("請問怎麼聯絡客服？") must not
# hand the conversation over. In order:
# 1. no keyword, no request phrase                      -> no handoff
# 2. a support-information word (HANDOFF_INFO_WORDS)    -> no handoff (the AI answers)
# 3. an English phrase keyword or a request phrase      -> handoff
# 4. a Chinese keyword in a short message               -> handoff
# 5. a Chinese keyword in a longer question (?/？)       -> no handoff; otherwise handoff
# A request phrase is a request verb, optionally a linking word, then a
# person word that appears in one of the channel's keywords ("幫我轉客服",
# "我想找真人"), so custom keyword lists keep their own meaning.
HANDOFF_SHORT_MAX_CHARS = 20  # letters and digits, punctuation and spaces not counted
HANDOFF_REQUEST_VERBS = ('我要', '我想', '想要', '幫我', '請', '麻煩', '給我', '轉', '找', '接', '換', '叫')
HANDOFF_REQUEST_LINKS = ('一下', '個', '給', '成', '到', '找', '轉', '接', '聯絡', '聯繫')
HANDOFF_PERSON_WORDS = ('真人', '人工', '客服', '專人')
# Not ambiguous words such as 還是 ("or" / "still": 我還是要找真人) or 如果
# (如果可以，請幫我轉真人), which also appear in real requests.
HANDOFF_INFO_WORDS = (
    '怎麼', '怎樣', '如何', '電話', '號碼', '幾號', '幾點', '時間', '時段', '上班', '下班', '營業',
    '多少', '哪些', '哪裡', '哪個', '什麼', '甚麼', '資料', '評價', '態度', '方式', '管道', '信箱',
    'email', 'e-mail',
)
_HANDOFF_NOT_COUNTED_RE = re.compile(r'[\W_]+')
_PAUSE_NOTICE_PLACEHOLDER_RE = re.compile(r'%\(minutes\)[sd]')

# Reasoning blocks and special tokens some models leave in the answer.
_THINK_RE = re.compile(r'<(think|thinking)>.*?</\1\s*>', re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r'<(?:think|thinking)>.*$', re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r'^.*?</(?:think|thinking)\s*>', re.DOTALL | re.IGNORECASE)
_SPECIAL_TOKEN_RE = re.compile(r'<\|[a-z0-9_]{1,40}\|>', re.IGNORECASE)


class LLMNotConfigured(Exception):
    """Endpoint URL or access token of the channel missing or not allowed."""


class LLMInvalidResponse(ValueError):
    """The backend answered 200 but not with the expected JSON shape."""


class LLMEmptyResponse(ValueError):
    """The backend answered with no visible content."""


# Short operator-facing explanation per HTTP status (Traditional Chinese).
_HTTP_STATUS_MESSAGES = {
    400: '請求格式或模型名稱有誤',
    401: '金鑰或簽章無效',
    403: '後端拒絕存取（權限不足）',
    404: '網址或模型不存在',
    408: '上游服務逾時',
    413: '對話內容太長',
    429: '呼叫太頻繁或額度已用完',
}


def endpoint_url_allowed(url):
    """https://, or http:// to localhost, 127.0.0.1, ::1 or an in-cluster
    host ending in .svc.cluster.local (a short service name is refused)."""
    try:
        parts = urlsplit((url or '').strip())
        host = (parts.hostname or '').lower()
    except ValueError:
        return False
    if not host:
        return False
    scheme = (parts.scheme or '').lower()
    if scheme == 'https':
        return True
    if scheme == 'http':
        return host in HTTP_ALLOWED_HOSTS or host.endswith(HTTP_ALLOWED_SUFFIX)
    return False


def strip_think(text):
    """Remove reasoning blocks (<think>, <thinking>, also unclosed) and special tokens."""
    if not text:
        return ''
    text = _SPECIAL_TOKEN_RE.sub('', text)
    text = _THINK_RE.sub('', text)
    text = _THINK_OPEN_RE.sub('', text)
    text = _THINK_CLOSE_RE.sub('', text)
    return text.strip()


def json_dumps(payload):
    """Compact UTF-8 JSON: the exact bytes that are signed and sent to n8n."""
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


def n8n_signature_headers(secret, body, timestamp=None):
    """n8n request signature: ``sha256=`` + hex HMAC-SHA256(secret,
    "<timestamp>.<body bytes>"). The secret itself is never sent."""
    timestamp = str(int(timestamp if timestamp is not None else time.time()))
    digest = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    return {'X-Woow-Timestamp': timestamp, 'X-Woow-Signature': 'sha256=%s' % digest}


def request_not_sent(error):
    """True when a requests.ConnectionError happened before the request was
    sent: a connect timeout or a connection that could not be opened
    (refused, unknown host). A connection reset or closed after the request
    was sent is not: the backend may already be working on it."""
    if isinstance(error, requests.ConnectTimeout):
        return True
    if isinstance(error, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if not isinstance(error, requests.ConnectionError):
        return False
    reason = error.args[0] if getattr(error, 'args', None) else None
    if isinstance(reason, MaxRetryError):
        reason = reason.reason
    return isinstance(reason, NewConnectionError)


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
    are not supported (only the text is posted)."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]  # "Respond to Webhook" with all incoming items
    if not isinstance(data, dict):
        raise LLMInvalidResponse('invalid response: not a JSON object')
    if data.get('skip') is True:
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
    if not texts:
        raise LLMEmptyResponse('empty reply')
    return texts, False


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


class ImLivechatChannel(models.Model):
    """Live Chat channel with an optional AI backend (per channel)."""
    _inherit = 'im_livechat.channel'

    # --- AI Integration Fields ---
    ai_enabled = fields.Boolean(
        string='Enable AI Integration',
        groups=AI_CONFIG_GROUP,
        default=False,
        help='Answer visitors through the AI backend of this channel.',
    )
    # Readable by Live Chat managers, changed by system administrators only
    # (see _check_ai_config_access).
    ai_backend_type = fields.Selection(
        selection=[
            (BACKEND_OPENAI, 'OpenAI compatible (LiteLLM, Hermes…)'),
            (BACKEND_N8N, 'n8n Webhook'),
        ],
        string='Backend Type',
        groups=AI_CONFIG_GROUP,
        default=BACKEND_OPENAI,
        required=True,
        help='OpenAI compatible: any server with /v1/chat/completions, for example '
             'LiteLLM or Hermes. n8n Webhook: an n8n workflow that starts with a '
             'Webhook node and answers with "Respond to Webhook".',
    )
    ai_endpoint_url = fields.Char(
        string='Endpoint URL',
        groups=SYSTEM_GROUP,
        copy=False,
        help='OpenAI compatible: the base URL ending in /v1 (for example '
             'https://litellm.example.com/v1); /chat/completions is added. '
             'n8n: the full production webhook URL. Must start with https://; '
             'http:// is only accepted for localhost, 127.0.0.1, ::1 and '
             'in-cluster hosts ending in .svc.cluster.local.',
    )
    ai_api_token = fields.Char(
        string='Access Token',
        groups=SYSTEM_GROUP,
        compute='_compute_ai_api_token',
        inverse='_inverse_ai_api_token',
        store=False,
        copy=False,
        help='OpenAI compatible (required): the API key, sent as "Authorization: Bearer '
             '<token>". n8n (optional): the signing secret of the X-Woow-Signature header '
             '(HMAC-SHA256); it is never sent. Without it requests are not signed (a public '
             'Chat Trigger). The saved token is never shown again; leave the field empty '
             'to keep it.',
    )
    ai_api_token_set = fields.Boolean(
        string='Access Token Saved',
        groups=SYSTEM_GROUP,
        compute='_compute_ai_api_token',
    )
    ai_connection_ready = fields.Boolean(
        string='Backend Connection Ready',
        groups=AI_CONFIG_GROUP,
        compute='_compute_ai_connection_ready',
        help='The endpoint URL is set, and for OpenAI compatible backends the access '
             'token and the model too.',
    )
    ai_model = fields.Char(
        string='Model (optional)',
        groups=AI_CONFIG_GROUP,
        help='OpenAI compatible backends: the model name the backend expects (LiteLLM '
             'needs it, for example chatgpt-pool/gpt-6-sol; Hermes ignores it). '
             'Not sent when empty. Not used by n8n.',
    )
    ai_document_mode = fields.Selection(
        selection=[
            (DOCUMENT_MODE_FILE, 'Attach the file (LiteLLM / OpenAI)'),
            (DOCUMENT_MODE_LINK, 'Download link only (Hermes…)'),
        ],
        string='Visitor Files',
        groups=AI_CONFIG_GROUP,
        default=DOCUMENT_MODE_FILE,
        help='Attach the file: LiteLLM / OpenAI read PDF, Excel and Word files directly. '
             'Download link only: for Hermes and other backends that do not take file '
             'content; the backend downloads the file itself. Images are attached in both '
             'modes; n8n always gets download links.',
    )
    ai_system_prompt = fields.Text(
        string='System Prompt',
        groups=AI_CONFIG_GROUP,
        help='Optional instructions sent to the backend with every request. Leave '
             'empty to let the backend decide the persona.',
    )
    ai_max_history = fields.Integer(
        string='Max History Messages',
        groups=AI_CONFIG_GROUP,
        default=50,
        help='Maximum number of recent messages to include as conversation context (1-200)',
    )
    ai_max_retries = fields.Integer(
        string='Max Retries',
        groups=AI_CONFIG_GROUP,
        default=2,
        help='Retries after a failed backend call (0-2), after 3 seconds and then 10 '
             'seconds; each call waits "Backend Reply Timeout" seconds. n8n calls are only '
             'retried when the connection could not be opened or n8n answered HTTP 429 or 503.',
    )
    ai_request_timeout = fields.Integer(
        string='Backend Reply Timeout (seconds)',
        groups=AI_CONFIG_GROUP,
        default=0,
        help='0 = default (OpenAI compatible 45 seconds, n8n 120 seconds). Otherwise 10-300 '
             'seconds per call. n8n calls are never retried after a timeout.',
    )
    ai_error_message = fields.Text(
        string='Error Message',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_ERROR_MESSAGE,
        help='Message sent to visitor when the AI API call fails after all retries',
    )
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
        default=DEFAULT_HANDOFF_KEYWORDS,
        help='Comma-separated words or phrases. When a visitor message contains one '
             'of them, the AI sends the handoff message once, waits for an operator '
             'and notifies the operators. Chinese entries match anywhere in the '
             'message; English entries match whole words or phrases only, '
             'case-insensitive.',
    )
    ai_pause_notice = fields.Text(
        string='Waiting Notice',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_PAUSE_NOTICE,
        help='Sent once to the first visitor message written while the AI waits for '
             'an operator after a handoff request. %(minutes)s is replaced with the '
             'wait minutes.',
    )
    ai_welcome_message = fields.Text(
        string='AI Welcome Message',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_WELCOME_MESSAGE,
        help='Shown as the welcome message of the chat window while AI is enabled '
             '(instead of the channel welcome message), so visitors know they are '
             'talking to an AI.',
    )
    ai_handoff_message = fields.Text(
        string='Handoff Message',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_HANDOFF_MESSAGE,
        help='Sent to the visitor when they ask for a human operator',
    )
    ai_bot_partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Bot Partner',
        groups=AI_CONFIG_GROUP,
        ondelete='set null',
        copy=False,
        help='The partner record used as the author for AI-generated messages '
             '(created by the module; it cannot be chosen)',
    )
    ai_bot_name = fields.Char(
        string='Bot Name',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_BOT_NAME,
        help='Display name for the AI bot in chat',
    )
    ai_log_payload = fields.Boolean(
        string='Log Conversation Content',
        groups=SYSTEM_GROUP,
        default=False,
        copy=False,
        help='Keep the requests sent to and the answers received from the backend in '
             'the AI API logs of this channel. Turn on only while debugging; turning it '
             'off erases the stored content.',
    )
    ai_log_retention_days = fields.Integer(
        string='Log Retention (days)',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_LOG_RETENTION_DAYS,
        help='AI API logs and finished reply jobs of this channel older than this are '
             'deleted by the daily cleanup job.',
    )

    # --- Reply job monitoring (P1-6), shown on the AI tab ---
    ai_cron_lastcall = fields.Datetime(
        string='Recovery Cron Last Run',
        groups=AI_CONFIG_GROUP,
        compute='_compute_ai_monitoring',
    )
    ai_stale_job_count = fields.Integer(
        string='Unfinished Jobs Older Than 5 Minutes',
        groups=AI_CONFIG_GROUP,
        compute='_compute_ai_monitoring',
    )
    ai_job_warning = fields.Text(
        string='Reply Job Warning',
        groups=AI_CONFIG_GROUP,
        compute='_compute_ai_monitoring',
    )

    # --- Access token storage (SQL-only column) ---

    def _auto_init(self):
        res = super()._auto_init()
        self.env.cr.execute(SQL(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s VARCHAR",
            SQL.identifier(self._table), SQL.identifier(TOKEN_COLUMN),
        ))
        return res

    def _ai_get_tokens(self):
        """{channel id: token} read with SQL; never expose the values."""
        ids = tuple({record._origin.id or record.id for record in self} - {False})
        ids = tuple(i for i in ids if isinstance(i, int))
        if not ids:
            return {}
        self.env.cr.execute(SQL(
            "SELECT id, %s FROM %s WHERE id IN %s",
            SQL.identifier(TOKEN_COLUMN), SQL.identifier(self._table), ids,
        ))
        return {row[0]: row[1] or '' for row in self.env.cr.fetchall()}

    def _ai_get_token(self):
        self.ensure_one()
        return (self._ai_get_tokens().get(self._origin.id or self.id) or '').strip()

    def _ai_set_token(self, token):
        ids = tuple(i for i in self.ids if isinstance(i, int))
        if not ids:
            return
        self.env.cr.execute(SQL(
            "UPDATE %s SET %s = %s WHERE id IN %s",
            SQL.identifier(self._table), SQL.identifier(TOKEN_COLUMN), token or None, ids,
        ))

    def _compute_ai_api_token(self):
        tokens = self.sudo()._ai_get_tokens()
        for channel in self:
            # Write-only: the stored token is never sent back to the browser.
            channel.ai_api_token = False
            channel.ai_api_token_set = bool(tokens.get(channel._origin.id or channel.id))

    def _inverse_ai_api_token(self):
        for channel in self:
            token = (channel.ai_api_token or '').strip()
            if token:
                channel._check_system_admin()
                channel._ai_set_token(token)
                _logger.info("Live chat channel %s: AI access token updated by user %s",
                             channel.id, self.env.uid)
        # The typed token must not be echoed back by web_save's read.
        self.invalidate_recordset(['ai_api_token', 'ai_api_token_set', 'ai_connection_ready'])

    @api.depends('ai_backend_type', 'ai_endpoint_url', 'ai_model')
    def _compute_ai_connection_ready(self):
        for channel in self:
            channel.ai_connection_ready = channel._ai_connection_is_ready()

    @api.depends('ai_enabled')
    def _compute_ai_monitoring(self):
        Job = self.env['im_livechat.ai.job']
        cron = self.env.ref('im_livechat_ai.ir_cron_recover_ai_jobs', raise_if_not_found=False)
        cron = cron.sudo() if cron else cron
        lastcall = cron.lastcall if cron else False
        stale = Job._stale_job_count(MONITOR_STALE_MINUTES)
        warnings = []
        if not cron or not cron.active:
            warnings.append(_('排程「Live Chat AI: Recover Reply Jobs」已停用，重啟或部署後遺留的 AI 回覆不會補送。'))
        elif not lastcall or lastcall < fields.Datetime.now() - timedelta(minutes=MONITOR_STALE_MINUTES):
            warnings.append(_(
                '排程「Live Chat AI: Recover Reply Jobs」超過 %(minutes)s 分鐘沒有執行。'
                '請確認 Odoo 的排程程序（cron worker，max_cron_threads ≥ 1）正在執行；'
                '重啟或部署後的回覆與等候真人後的自動回覆都要靠它送出。',
                minutes=MONITOR_STALE_MINUTES,
            ))
        if stale:
            warnings.append(_(
                '有 %(count)s 個 AI 回覆工作超過 %(minutes)s 分鐘仍未完成，請到「AI 回覆工作」查看。',
                count=stale, minutes=MONITOR_STALE_MINUTES,
            ))
        text = '\n'.join(warnings) or False
        for channel in self:
            channel.ai_cron_lastcall = lastcall
            channel.ai_stale_job_count = stale
            channel.ai_job_warning = text

    # --- Connection (read through sudo; callers never expose the token) ---

    def _ai_connection(self):
        """(backend type, endpoint URL, token) of this channel.

        openai URLs are normalized: no trailing slash, no /chat/completions.
        """
        self.ensure_one()
        channel = self.sudo()
        backend = channel.ai_backend_type or BACKEND_OPENAI
        url = (channel.ai_endpoint_url or '').strip()
        if backend == BACKEND_OPENAI:
            url = url.rstrip('/')
            if url.endswith('/chat/completions'):
                url = url[:-len('/chat/completions')].rstrip('/')
        return backend, url, channel._ai_get_token()

    def _ai_connection_is_ready(self):
        """URL set (allowed scheme/host); for openai also the token and the
        model. The n8n token is optional (no signature without it)."""
        self.ensure_one()
        backend, url, token = self._ai_connection()
        if not url or not endpoint_url_allowed(url):
            return False
        if backend == BACKEND_N8N:
            return True
        return bool(token and (self.sudo().ai_model or '').strip())

    # --- Constraints (P1-1: checked on every create/write, AI on or off) ---

    @api.constrains('ai_endpoint_url')
    def _check_ai_endpoint_url(self):
        for channel in self.sudo():
            url = (channel.ai_endpoint_url or '').strip()
            if url and not endpoint_url_allowed(url):
                raise ValidationError(_(
                    '端點網址必須以 https:// 開頭（只有 localhost、127.0.0.1、::1 與 '
                    '*.svc.cluster.local 的主機可以用 http://）。'
                ))

    @api.constrains('ai_max_history')
    def _check_ai_max_history(self):
        """Validate max history range."""
        for record in self:
            if record.ai_max_history < 1 or record.ai_max_history > MAX_AI_MAX_HISTORY:
                raise ValidationError(_('Max History Messages must be between 1 and 200.'))

    @api.constrains('ai_max_retries')
    def _check_ai_max_retries(self):
        """Validate max retries range."""
        for record in self:
            if record.ai_max_retries < 0 or record.ai_max_retries > LLM_MAX_RETRIES:
                raise ValidationError(_('Max Retries must be between 0 and 2.'))

    @api.constrains('ai_request_timeout')
    def _check_ai_request_timeout(self):
        for record in self:
            seconds = record.ai_request_timeout or 0
            if seconds and not MIN_REQUEST_TIMEOUT <= seconds <= MAX_REQUEST_TIMEOUT:
                raise ValidationError(_('等待後端回覆的秒數必須是 0（預設）或介於 10 到 300 之間。'))

    @api.constrains('ai_handoff_pause_minutes')
    def _check_ai_handoff_pause_minutes(self):
        for record in self:
            minutes = record.ai_handoff_pause_minutes
            if minutes < 1 or minutes > MAX_HANDOFF_PAUSE_MINUTES:
                raise ValidationError(_('暫停分鐘數必須介於 1 到 1440 之間。'))

    @api.constrains('ai_handoff_wait_minutes')
    def _check_ai_handoff_wait_minutes(self):
        for record in self:
            minutes = record.ai_handoff_wait_minutes
            if minutes < 1 or minutes > MAX_HANDOFF_PAUSE_MINUTES:
                raise ValidationError(_('等候真人客服的分鐘數必須介於 1 到 1440 之間。'))

    @api.constrains('ai_log_retention_days')
    def _check_ai_log_retention_days(self):
        for record in self:
            days = record.ai_log_retention_days
            if days < MIN_LOG_RETENTION_DAYS or days > MAX_LOG_RETENTION_DAYS:
                raise ValidationError(_('日誌保留天數必須介於 1 到 3650 之間。'))

    # Re-checked when AI is enabled (values stored before these checks
    # existed, e.g. by an earlier version, must not reach the backend).
    _AI_RANGE_CHECKS = (
        '_check_ai_max_history',
        '_check_ai_max_retries',
        '_check_ai_request_timeout',
        '_check_ai_handoff_pause_minutes',
        '_check_ai_handoff_wait_minutes',
    )

    def _ai_check_all_ranges(self):
        for method in self._AI_RANGE_CHECKS:
            getattr(self, method)()

    def _ai_check_enable_requirements(self):
        """An AI-enabled channel needs a complete backend connection.

        Runs as the saving user (e.g. a Live Chat manager); the connection is
        only probed through sudo and only for presence: no value reaches the
        error message.
        """
        for channel in self.sudo():
            if not channel.ai_enabled:
                continue
            backend, url, token = channel._ai_connection()
            if not url or (backend == BACKEND_OPENAI and not token) or not endpoint_url_allowed(url):
                raise UserError(_(
                    '頻道「%(name)s」尚未設定後端連線。請系統管理員在頻道的「AI」分頁填寫'
                    '端點網址與存取金鑰（Token）後，再啟用 AI。',
                    name=channel.name,
                ))
            if backend == BACKEND_OPENAI and not (channel.ai_model or '').strip():
                raise UserError(_('OpenAI 相容後端啟用 AI 時必須填寫模型。'))

    # --- CRUD: the bot partner exists as soon as AI is on (P2-6) ---

    @api.model
    def _check_ai_config_access(self, vals, records=None):
        """Only Live Chat managers configure the AI, only system
        administrators set the backend connection (type, URL, token,
        content logging), and nobody but the module itself chooses the bot
        partner (clearing it is allowed: a new bot is created when needed).

        Field groups already stop reads and writes of the ai_* fields, but
        Odoo does not check field groups on create, so create checks here.
        A manager may send the backend type unchanged (or with a new
        channel, which has no URL or token yet).
        """
        if self.env.su:
            return
        names = {name for name in vals if name.startswith('ai_') and name in self._fields}
        if not names:
            return
        if vals.get('ai_bot_partner_id'):
            raise AccessError(_('AI 機器人的聯絡人由系統自動建立，不能指定。'))
        if not self.env.user.has_group(AI_CONFIG_GROUP):
            raise AccessError(_('只有 Live Chat 管理員可以設定 AI。'))
        restricted = names & AI_SYSTEM_FIELDS
        if not restricted or self.env.user.has_group(SYSTEM_GROUP):
            return
        if 'ai_backend_type' in restricted:
            current = records.sudo().mapped('ai_backend_type') if records is not None else []
            if all(value == vals['ai_backend_type'] for value in current):
                restricted.discard('ai_backend_type')
        if restricted:
            raise AccessError(_('只有系統管理員可以設定 AI 後端連線（類型、網址、金鑰、記錄對話內容）。'))

    @api.model
    def _check_ai_manager(self):
        if not self.env.is_superuser() and not self.env.user.has_group(AI_CONFIG_GROUP):
            raise AccessError(_('只有 Live Chat 管理員可以使用此功能。'))

    @api.model
    def _check_system_admin(self):
        if not self.env.su and not self.env.user.has_group(SYSTEM_GROUP):
            raise AccessError(_('只有系統管理員可以管理 AI 後端連線。'))

    @staticmethod
    def _ai_clean_url_vals(vals):
        if 'ai_endpoint_url' in vals:
            vals['ai_endpoint_url'] = (vals['ai_endpoint_url'] or '').strip() or False
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._ai_clean_url_vals(dict(vals)) for vals in vals_list]
        for vals in vals_list:
            self._check_ai_config_access(vals)
        channels = super().create(vals_list)
        # sudo: an operator may create a channel (AI off); the ai_* fields
        # are manager-only.
        enabled = channels.sudo().filtered('ai_enabled')
        enabled._ai_check_enable_requirements()
        enabled._get_or_create_bot_partner()
        return channels

    def write(self, vals):
        self._check_ai_config_access(vals, records=self)
        vals = self._ai_clean_url_vals(dict(vals))
        current = self.sudo()
        enabling = current.filtered(lambda c: not c.ai_enabled) if vals.get('ai_enabled') else current.browse()
        erase = current.browse()
        if 'ai_log_payload' in vals and not vals['ai_log_payload']:
            erase = current.filtered('ai_log_payload')
        res = super().write(vals)
        if enabling:
            # P1-1: enabling AI re-validates every AI setting.
            enabling._ai_check_all_ranges()
        if {'ai_enabled', 'ai_model', 'ai_backend_type', 'ai_endpoint_url'} & set(vals):
            current._ai_check_enable_requirements()
        if erase:
            # Turning content logging off also erases what was captured.
            self.env['llm.api.log'].sudo()._erase_payloads(erase.ids)
        if 'ai_enabled' in vals or 'ai_bot_name' in vals:
            current.filtered('ai_enabled')._get_or_create_bot_partner()
        return res

    def copy_data(self, default=None):
        """A copy starts with AI off and without the endpoint URL and the
        access token (never duplicated; a system administrator sets them on
        the copy)."""
        vals_list = super().copy_data(default=default)
        for vals in vals_list:
            if not (default or {}).get('ai_enabled'):
                vals['ai_enabled'] = False
        return vals_list

    # --- Live chat window (AI disclosure, P1-7) ---

    def _get_channel_infos(self):
        """Visitors of an AI-enabled channel see the AI welcome message
        (rendered by the widget as the first message of the chat)."""
        infos = super()._get_channel_infos()
        channel = self.sudo()
        if channel.ai_enabled:
            infos['default_message'] = channel.ai_welcome_message or DEFAULT_WELCOME_MESSAGE
        return infos

    # --- Bot Partner Management ---

    @api.model
    def _ai_bot_category(self):
        return self.env.ref(BOT_CATEGORY_XMLID, raise_if_not_found=False)

    def _ai_is_own_bot_partner(self, partner):
        """True when `partner` is this channel's own AI bot: tagged "AI 機器人",
        without any user, and not the bot of another channel. Only such a
        partner is ever renamed or tagged through sudo."""
        self.ensure_one()
        partner = partner.sudo().with_context(active_test=False)
        category = self._ai_bot_category()
        if not category or category not in partner.category_id or partner.user_ids:
            return False
        if partner == self.env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False):
            return False  # the module's shared fallback bot
        others = self.sudo().with_context(active_test=False).search_count([
            ('ai_bot_partner_id', '=', partner.id), ('id', '!=', self.id),
        ], limit=1)
        return not others

    def _get_or_create_bot_partner(self):
        """
        Ensure each channel has its bot partner (called when AI is enabled or
        the bot name changes, never while a reply is processed: P2-6).

        The partner is archived, like Odoo's chatbot partners, so it does not
        appear in the contact list, and tagged "AI 機器人" so it can be
        recognised. An existing partner is renamed when the bot name changed;
        a linked partner that is not this channel's own bot (a real person,
        a user, another channel's bot) is never touched: the channel gets a
        new bot partner instead.

        Returns:
            res.partner: The bot partner record(s)
        """
        partners = self.env['res.partner']
        category = self._ai_bot_category()
        for channel in self:
            bot_name = channel.ai_bot_name or DEFAULT_BOT_NAME
            partner = channel.ai_bot_partner_id
            if partner and not channel._ai_is_own_bot_partner(partner):
                _logger.warning(
                    "Live chat channel %s: partner %s is not its own AI bot; creating a new bot partner",
                    channel.id, partner.id,
                )
                partner = self.env['res.partner']
            if partner:
                vals = {}
                if partner.name != bot_name:
                    vals['name'] = bot_name
                if category and category not in partner.sudo().category_id:
                    vals['category_id'] = [(4, category.id)]
                if not partner.sudo().image_1920:
                    vals['image_1920'] = self._ai_bot_avatar()
                if vals:
                    partner.sudo().write(vals)
            else:
                partner = self.env['res.partner'].sudo().create({
                    'name': bot_name,
                    'active': False,
                    'is_company': False,
                    'type': 'contact',
                    'category_id': [(4, category.id)] if category else [],
                    'image_1920': self._ai_bot_avatar(),
                })
                channel.sudo().write({'ai_bot_partner_id': partner.id})
            partners |= partner
        return partners

    @api.model
    def _ai_bot_avatar(self):
        """Base64 avatar of the AI bot partners (shipped with the module)."""
        with tools.file_open(BOT_AVATAR_PATH, 'rb') as avatar:
            return base64.b64encode(avatar.read())

    def _get_bot_partner(self):
        """Author of AI messages, read-only (no write during reply processing).

        Falls back to the module's default bot partner when the channel has
        none (e.g. it was deleted after AI was enabled).
        """
        self.ensure_one()
        if self.ai_bot_partner_id:
            return self.ai_bot_partner_id
        _logger.warning("Live chat channel %s has no AI bot partner; using the default one", self.id)
        return self.env.ref('im_livechat_ai.partner_ai_bot')

    @api.model
    def _ai_bot_partner_ids(self, channel=None):
        """Partners that are the AI itself: the channel bot and the default bot."""
        ids = set()
        if channel and channel.ai_bot_partner_id:
            ids.add(channel.ai_bot_partner_id.id)
        default_bot = self.env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False)
        if default_bot:
            ids.add(default_bot.id)
        return ids

    # --- Human handoff (P1-2) ---

    def _ai_handoff_keyword_list(self):
        """Keywords, lower case, inner whitespace collapsed to one space."""
        self.ensure_one()
        raw = self.ai_handoff_keywords or ''
        return [' '.join(k.lower().split()) for k in re.split(r'[,，、\n]', raw) if k.strip()]

    @staticmethod
    def _ai_phrase_in(keyword, text):
        """English keyword: whole words, any whitespace between them."""
        phrase = r'\s+'.join(re.escape(word) for word in keyword.split(' '))
        return bool(re.search(r'(?<![0-9a-z_])' + phrase + r'(?![0-9a-z_])', text))

    @staticmethod
    def _ai_handoff_request_re(keywords):
        """Request phrase regex for these keywords, or None (see HANDOFF_PERSON_WORDS)."""
        persons = [word for word in HANDOFF_PERSON_WORDS if any(word in keyword for keyword in keywords)]
        if not persons:
            return None
        def alternatives(words):
            return '|'.join(re.escape(word) for word in sorted(words, key=len, reverse=True))
        return re.compile('(?:%s)(?:%s)?(?:%s)' % (
            alternatives(HANDOFF_REQUEST_VERBS), alternatives(HANDOFF_REQUEST_LINKS), alternatives(persons)))

    def _ai_is_handoff_request(self, text):
        """True when the visitor asks for a human (rules at HANDOFF_SHORT_MAX_CHARS).

        Keywords: Chinese (any non-Latin) entries match anywhere in the text,
        English words and phrases as whole words, case-insensitive, any
        whitespace between the words. A question that only mentions the
        support service ("如果要聯絡客服，一般需要準備哪些資料？") is answered
        by the AI instead."""
        self.ensure_one()
        text = (text or '').lower().strip()
        if not text:
            return False
        keywords = self._ai_handoff_keyword_list()
        english = any(self._ai_phrase_in(k, text) for k in keywords if _WORD_CHARS_RE.match(k))
        chinese = any(k in text for k in keywords if not _WORD_CHARS_RE.match(k))
        request_re = self._ai_handoff_request_re(keywords)
        request = bool(request_re and request_re.search(text))
        if not (english or chinese or request):
            return False
        if any(word in text for word in HANDOFF_INFO_WORDS):
            return False
        if english or request:
            return True
        if len(_HANDOFF_NOT_COUNTED_RE.sub('', text)) <= HANDOFF_SHORT_MAX_CHARS:
            return True
        return not text.endswith(('?', '？'))

    def _ai_pause_minutes(self):
        """Pause after an operator's message (minutes)."""
        self.ensure_one()
        minutes = self.ai_handoff_pause_minutes or DEFAULT_HANDOFF_PAUSE_MINUTES
        return max(1, min(minutes, MAX_HANDOFF_PAUSE_MINUTES))

    def _ai_wait_minutes(self):
        """Wait for an operator after a visitor's handoff request (minutes)."""
        self.ensure_one()
        minutes = self.ai_handoff_wait_minutes or DEFAULT_HANDOFF_WAIT_MINUTES
        return max(1, min(minutes, MAX_HANDOFF_PAUSE_MINUTES))

    def _ai_pause_notice_text(self):
        """The waiting notice with %(minutes)s replaced by the wait minutes.

        Plain replacement, not %-formatting: an administrator's text with
        other % signs or placeholders is sent as written and never raises.
        The bot message escapes the text when it is posted.
        """
        self.ensure_one()
        text = self.ai_pause_notice or DEFAULT_PAUSE_NOTICE
        return _PAUSE_NOTICE_PLACEHOLDER_RE.sub(str(self._ai_wait_minutes()), text)

    # --- Runtime bounds (P1-1: legacy or hand-edited values never reach the LLM) ---

    def _ai_max_history_value(self):
        self.ensure_one()
        return max(1, min(self.ai_max_history or DEFAULT_AI_MAX_HISTORY, MAX_AI_MAX_HISTORY))

    def _ai_request_timeout_value(self):
        """Seconds a chat call waits: ai_request_timeout (10-300), or 45 s
        (OpenAI compatible) / 120 s (n8n) when it is 0 or empty."""
        self.ensure_one()
        channel = self.sudo()
        seconds = channel.ai_request_timeout or 0
        if seconds <= 0:
            return N8N_REQUEST_TIMEOUT if channel.ai_backend_type == BACKEND_N8N else LLM_REQUEST_TIMEOUT
        return max(MIN_REQUEST_TIMEOUT, min(seconds, MAX_REQUEST_TIMEOUT))

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
        recovery cron if that thread is lost.

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

        if not self.ai_enabled:
            return Job

        if kind in ('handoff', 'notice'):
            return Job._create_for_message(discuss_channel, message, kind=kind)

        if not self._ai_connection_is_ready():
            _logger.warning(
                "AI integration incomplete for live chat channel %s: backend connection "
                "(endpoint URL, access token or model) missing", self.id,
            )
            return Job

        return Job._create_for_message(discuss_channel, message, run_after=run_after)

    # --- Backend requests ---

    @api.model
    def _ai_session_id(self, discuss_channel):
        """Stable id of one conversation: ``woow-ai-<db uuid 8 hex>-<discuss.channel id>``
        (the same form as woow_ai_contact)."""
        uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid') or ''
        return 'woow-ai-%s-%s' % (uuid.replace('-', '')[:8], discuss_channel.id)

    @api.model
    def _ai_openai_metadata(self, session_id, identity, livechat_channel_name):
        """OpenAI ``metadata``: string values only (informational; LiteLLM and
        Hermes do not pass it to the model). No email."""
        metadata = {'session_id': session_id}
        for key in ('partner_id', 'guest_id', 'name', 'lang', 'is_portal', 'is_guest'):
            value = identity.get(key)
            if value in (None, '', False) and not isinstance(value, bool):
                continue
            if isinstance(value, bool):
                value = 'true' if value else 'false'
            metadata['odoo_%s' % key] = str(value)[:METADATA_MAX_VALUE]
        if livechat_channel_name:
            metadata['livechat_channel'] = livechat_channel_name[:METADATA_MAX_VALUE]
        return metadata

    def _ai_build_request(self, discuss_channel, conversation, trigger_message=None):
        """The HTTP request of one reply (reads the database; no HTTP).

        Returns:
            dict: backend, url, headers, payload (the JSON body as a dict),
                body (bytes, n8n), token (n8n signing secret, in memory only)
        Raises:
            LLMNotConfigured: URL or token missing, or the URL not allowed
        """
        self.ensure_one()
        channel = self.sudo()
        backend, url, token = channel._ai_connection()
        if not url or (backend == BACKEND_OPENAI and not token):
            raise LLMNotConfigured('endpoint URL or access token missing')
        if not endpoint_url_allowed(url):
            raise LLMNotConfigured('endpoint URL not allowed (https only, http for local hosts)')
        session = discuss_channel.sudo()
        session_id = self._ai_session_id(session)
        identity = session._ai_visitor_identity()
        headers = {'Content-Type': 'application/json'}
        if backend == BACKEND_N8N:
            payload = channel._ai_n8n_payload(session, conversation, session_id, identity, trigger_message)
            return {
                'backend': backend, 'url': url, 'headers': headers, 'payload': payload,
                'body': json_dumps(payload).encode(), 'token': token,
                'timeout': channel._ai_request_timeout_value(),
            }
        payload = {}
        model = (channel.ai_model or '').strip()
        if model:
            payload['model'] = model
        payload['messages'] = session._ai_openai_messages(conversation, livechat_channel=channel)
        payload['user'] = session_id
        payload['metadata'] = self._ai_openai_metadata(session_id, identity, channel.name or '')
        headers['Authorization'] = 'Bearer %s' % token
        return {
            'backend': backend, 'url': url + '/chat/completions', 'headers': headers,
            'payload': payload, 'body': None, 'token': None,
            'timeout': channel._ai_request_timeout_value(),
        }

    def _ai_n8n_payload(self, session, conversation, session_id, identity, trigger_message=None):
        """n8n webhook body (woow_ai_contact docs/PROTOCOL.md "n8n backend"),
        plus the fields an n8n Chat Trigger reads (``action``, ``sessionId``,
        ``chatInput``); a Webhook-node workflow ignores them."""
        self.ensure_one()
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
            'contact': {'id': self.id, 'name': self.name or ''},
            'user': identity,
            'channel': session._ai_channel_info(),
            'system_prompt': conversation['system_prompt'] or '',
            'message': message,
            'history': [{'role': e['role'], 'text': e['text'], 'attachments': attachments(e)}
                        for e in history if e is not current],
            'events': [{
                'time': event['time'].replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z'),
                'kind': event['kind'],
                'text': event['text'],
            } for event in conversation['events']],
        }

    @staticmethod
    def _ai_post(request):
        """One HTTP POST (no database access). n8n bodies are signed per call
        (fresh timestamp) when a signing secret is set; redirects are never
        followed. Waits request['timeout'] seconds (the channel's setting)."""
        headers = dict(request['headers'])
        n8n = request['backend'] == BACKEND_N8N
        timeout = request.get('timeout') or (N8N_REQUEST_TIMEOUT if n8n else LLM_REQUEST_TIMEOUT)
        kwargs = {'headers': headers, 'timeout': timeout, 'allow_redirects': False}
        if n8n:
            if request['token']:
                headers.update(n8n_signature_headers(request['token'], request['body']))
            kwargs['data'] = request['body']
        else:
            kwargs['json'] = request['payload']
        return requests.post(request['url'], **kwargs)

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
        """openai: every failure is retried (within ai_max_retries). n8n:
        only a connection that could not be opened and HTTP 429 / 503 (the
        workflow did not start); never a read timeout, a reset after the
        request was sent or any other answer (decision 26 of woow_ai_contact)."""
        if backend != BACKEND_N8N:
            return True
        if isinstance(error, requests.HTTPError):
            response = error.response
            return response is not None and response.status_code in N8N_RETRY_STATUSES
        return request_not_sent(error)

    def _generate_ai_reply(self, discuss_channel, conversation, before_call=None, trigger_message=None):
        """
        Produce one AI reply: one backend request, bounded retries, a log per call.

        Failed calls are retried at most ai_max_retries times (0-2), waiting
        LLM_RETRY_BACKOFF seconds (3 s, then 10 s); each call times out after
        the channel's ai_request_timeout (0: LLM_REQUEST_TIMEOUT 45 s, n8n
        N8N_REQUEST_TIMEOUT 120 s; at most MAX_REQUEST_TIMEOUT). n8n calls
        are retried only when they could not be delivered (see
        _ai_should_retry): never after a read timeout, since the agent may
        still be running.

        Args:
            discuss_channel (discuss.channel): The chat session
            conversation (dict): result of discuss.channel._ai_conversation()
            before_call (callable|None): Called right before each HTTP call
                (the job commits there so no transaction stays open)
            trigger_message (mail.message|None): the visitor message being
                answered (n8n ``message``)

        Returns:
            dict: replies (list of str), reply (first reply or None), skip
                (bool, n8n), error (short str or None), calls (int)
        """
        self.ensure_one()
        channel = self.sudo()
        result = {'replies': [], 'reply': None, 'skip': False, 'error': None, 'calls': 0}
        retries = max(0, min(channel.ai_max_retries, LLM_MAX_RETRIES))
        backend = channel.ai_backend_type or BACKEND_OPENAI
        log_model = (channel.ai_model or '').strip() if backend == BACKEND_OPENAI else False
        log_base = {
            'channel_id': channel.id,
            'discuss_channel_id': discuss_channel.id,
            'model_name': log_model or False,
            'backend_type': backend,
        }
        try:
            request = channel._ai_build_request(discuss_channel, conversation, trigger_message)
        except LLMNotConfigured as e:
            result['error'] = self._short_llm_error(e)
            channel._create_api_log(self.env, status='error', request_payload=None, response_payload=None,
                                    error_message=result['error'], **log_base)
            return result

        attempts = 0
        while True:
            if attempts:
                if before_call:
                    before_call()  # no transaction stays open during the wait
                time.sleep(LLM_RETRY_BACKOFF[min(attempts - 1, len(LLM_RETRY_BACKOFF) - 1)])
            attempts += 1
            result['calls'] += 1
            if before_call:
                before_call()  # after the last database read, right before the request
            start_time = time.time()
            http_status = None
            try:
                response = self._ai_post(request)
                http_status = getattr(response, 'status_code', None)
                texts, skip, data = self._ai_parse_response(backend, response)
                prompt, completion, total = usage_tokens(data.get('usage') if isinstance(data, dict) else None)
                channel._create_api_log(
                    self.env, status='success',
                    request_payload=request['payload'], response_payload=data,
                    prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                    response_time=time.time() - start_time, retry_count=attempts - 1,
                    http_status=http_status, **log_base,
                )
                result.update(replies=texts, reply=texts[0] if texts else None, skip=skip)
                return result
            except Exception as e:  # noqa: BLE001 - every failure is retried or reported
                response_time = time.time() - start_time
                # Full detail goes to the server log only; the stored log
                # keeps a short message without the upstream response body.
                last_error = self._short_llm_error(e)
                can_retry = attempts - 1 < retries and self._ai_should_retry(backend, e)
                _logger.warning(
                    "AI backend call failed for channel %s (call %s, %s): %s",
                    channel.id, attempts, 'retrying' if can_retry else 'giving up', last_error,
                )
                if can_retry:
                    channel._create_api_log(
                        self.env, status='retry', request_payload=request['payload'],
                        response_payload=None, response_time=response_time,
                        error_message=last_error, retry_count=attempts, http_status=http_status,
                        **log_base,
                    )
                    continue
                channel._create_api_log(
                    self.env, status='error', request_payload=request['payload'],
                    response_payload=None, response_time=response_time,
                    error_message=f'All {attempts} calls failed. Last error: {last_error}',
                    retry_count=attempts - 1, http_status=http_status, **log_base,
                )
                result['error'] = last_error
                return result

    @api.model
    def _describe_llm_error(self, error):
        """Map an exception to (status_code or None, short Chinese message).

        Never includes upstream response bodies, headers or the token.
        """
        if isinstance(error, requests.HTTPError):
            status = error.response.status_code if error.response is not None else None
            if status in _HTTP_STATUS_MESSAGES:
                return status, _HTTP_STATUS_MESSAGES[status]
            if status and 300 <= status < 400:
                return status, '後端要求轉址（請填最終網址，不會跟隨轉址）'
            if status and status >= 500:
                return status, 'AI 後端或上游服務暫時故障'
            return status, '後端回應錯誤'
        if isinstance(error, requests.Timeout):
            return None, '連線逾時'
        if isinstance(error, requests.ConnectionError):
            return None, '無法連線到 AI 後端'
        if isinstance(error, LLMNotConfigured):
            return None, '尚未設定 AI 後端連線（端點網址或存取金鑰）'
        if isinstance(error, LLMInvalidResponse):
            return None, '回應格式不正確'
        if isinstance(error, LLMEmptyResponse):
            return None, 'AI 回覆是空的'
        return None, '未預期的錯誤'

    @api.model
    def _short_llm_error(self, error):
        """Short error text stored in llm.api.log (no upstream body)."""
        status, message = self._describe_llm_error(error)
        prefix = f'HTTP {status}: ' if status else ''
        text = f'{prefix}{message} ({type(error).__name__})'
        return text[:MAX_LOG_ERROR_LENGTH]

    def _ai_test_notification(self, ok, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('AI 後端連線測試'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': not ok,
            },
        }

    def _create_bot_message(self, env, discuss_channel, body):
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

        Returns:
            mail.message: the posted message
        """
        self.ensure_one()

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

    def _create_api_log(self, env, channel_id, discuss_channel_id, model_name,
                        status, request_payload, response_payload,
                        prompt_tokens=0, completion_tokens=0, total_tokens=0,
                        response_time=None, error_message=None, retry_count=0,
                        http_status=None, backend_type=None):
        """
        Create an AI API log entry (never fails the caller).

        Conversation content (request and response bodies) is only kept when
        the channel's "Log Conversation Content" is on (default off, P2-1).
        The access token is never part of a payload, and the base64 data of
        inlined files is replaced by ``<N bytes base64 omitted>``.
        """
        try:
            vals = {
                'livechat_channel_id': channel_id,
                'model': model_name or False,
                'backend_type': backend_type or False,
                'status': status,
                'http_status': http_status or False,
                'response_time': response_time,
                'error_message': (error_message or '')[:MAX_LOG_ERROR_LENGTH] or False,
                'retry_count': retry_count,
                'prompt_tokens': prompt_tokens or 0,
                'completion_tokens': completion_tokens or 0,
                'total_tokens': total_tokens or 0,
            }

            # Validate FK references
            if discuss_channel_id and env['discuss.channel'].browse(discuss_channel_id).exists():
                vals['discuss_channel_id'] = discuss_channel_id

            channel = env['im_livechat.channel'].sudo().browse(channel_id).exists() if channel_id else None
            if channel and channel.ai_log_payload:
                if request_payload:
                    vals['request_payload'] = json.dumps(redact_base64(request_payload),
                                                         ensure_ascii=False, default=str)
                if response_payload:
                    vals['response_payload'] = json.dumps(redact_base64(response_payload),
                                                          ensure_ascii=False, default=str)

            env['llm.api.log'].sudo().create(vals)
        except Exception as log_error:
            _logger.error("Failed to create API log: %s", log_error, exc_info=True)

    # --- UI Action Methods ---

    def action_test_ai_connection(self):
        """
        Test this channel's backend connection (system administrators only).

        openai: a small chat completion with the channel's model. n8n: a
        ``{"event": "ping", ...}`` POST (signed when a secret is set, with
        ``action: loadPreviousSession`` for Chat Triggers); any HTTP 200 is a
        success.
        Shows only the HTTP status and a short Chinese message; details go
        to the server log, never the upstream answer or the token.
        """
        self.ensure_one()
        self._check_system_admin()
        channel = self.sudo()
        backend, url, token = channel._ai_connection()
        if not url or (backend == BACKEND_OPENAI and not token):
            raise UserError(_('請先填寫端點網址與存取金鑰（Token）並儲存，再測試連線。'))
        if not endpoint_url_allowed(url):
            raise UserError(_('端點網址必須以 https:// 開頭（只有 localhost、127.0.0.1、::1 與 '
                              '*.svc.cluster.local 的主機可以用 http://）。'))
        model = (channel.ai_model or '').strip()
        status = None
        try:
            if backend == BACKEND_N8N:
                # A Chat Trigger answers "loadPreviousSession" without running
                # the agent; a Webhook-node workflow answers the ping event.
                body = json_dumps({
                    'event': 'ping',
                    'contact': {'id': channel.id, 'name': channel.name or ''},
                    'action': 'loadPreviousSession',
                    'sessionId': 'woow-ai-ping-%s' % channel.id,
                }).encode()
                headers = {'Content-Type': 'application/json'}
                if token:
                    headers.update(n8n_signature_headers(token, body))
                response = requests.post(url, data=body, headers=headers,
                                         timeout=N8N_TEST_TIMEOUT, allow_redirects=False)
                status = response.status_code
                if status != 200:
                    raise requests.HTTPError('HTTP %s' % status, response=response)
                message = _('連線成功（HTTP %(status)s），n8n 已回應 ping。', status=status)
            else:
                if not model:
                    raise UserError(_('請先填寫模型，再測試連線。'))
                payload = {'model': model, 'messages': [{'role': 'user', 'content': CONNECTION_TEST_PROMPT}]}
                headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % token}
                response = requests.post(url + '/chat/completions', json=payload, headers=headers,
                                         timeout=LLM_REQUEST_TIMEOUT, allow_redirects=False)
                status = response.status_code
                self._ai_parse_response(backend, response)
                message = _('連線成功（HTTP %(status)s），模型 %(model)s 有回應。', status=status, model=model)
            ok = True
        except UserError:
            raise
        except Exception as e:  # noqa: BLE001 - every failure becomes a short notice
            _logger.warning("Live chat channel %s: AI backend connection test failed: %s",
                            channel.id, self._short_llm_error(e))
            ok = False
            status, short = self._describe_llm_error(e)
            if status:
                message = _('連線失敗（HTTP %(status)s）：%(reason)s', status=status, reason=short)
            else:
                message = _('連線失敗：%(reason)s', reason=short)
        _logger.info("Live chat channel %s: AI backend connection test by user %s: HTTP %s",
                     channel.id, self.env.uid, status)
        return self._ai_test_notification(ok, message)

    def action_view_api_logs(self):
        """Open the AI API logs of this channel."""
        self.ensure_one()
        self._check_ai_manager()

        return {
            'type': 'ir.actions.act_window',
            'name': _('AI API 日誌 - %s') % self.name,
            'res_model': 'llm.api.log',
            'view_mode': 'list,form',
            'domain': [('livechat_channel_id', '=', self.id)],
            'context': {
                'default_livechat_channel_id': self.id,
                'search_default_filter_last_7_days': 1,
            },
        }
