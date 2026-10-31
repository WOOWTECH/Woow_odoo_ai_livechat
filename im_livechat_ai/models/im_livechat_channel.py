# -*- coding: utf-8 -*-

import json
import logging
import re
import time
from urllib.parse import urlsplit

import base64

import requests
from markupsafe import Markup

from odoo import api, fields, models, tools, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

# --- System-level LiteLLM connection (ir.config_parameter keys) ---
# Only base.group_system can read or write ir.config_parameter, so a URL a
# non-admin could set is never combined with the API key.
ICP_BASE_URL = 'im_livechat_ai.base_url'
ICP_API_KEY = 'im_livechat_ai.api_key'
ICP_MODEL_CACHE = 'im_livechat_ai.model_cache'
ICP_LOG_PAYLOAD = 'im_livechat_ai.log_payload'
ICP_LOG_RETENTION_DAYS = 'im_livechat_ai.log_retention_days'

DEFAULT_BASE_URL = 'https://paas-cs-32e92be2-5b2e4e66.woowtech.io/v1'
DEFAULT_MODEL = 'chatgpt-pool/gpt-6-sol'
DEFAULT_LOG_RETENTION_DAYS = 14

LLM_REQUEST_TIMEOUT = 45  # seconds, chat completions (P1-7)
LLM_MAX_RETRIES = 2  # retries after the first call
LLM_RETRY_BACKOFF = (3, 10)  # seconds before retry 1 and retry 2
LLM_MODELS_TIMEOUT = 15  # seconds, GET /models
MAX_LOG_ERROR_LENGTH = 300

# --- Product lookup tools (package E, P1-8) ---
LLM_MAX_TOOL_ROUNDS = 3  # tool-calling rounds per reply, then a forced final answer
LLM_MAX_TOOL_CALLS = 3  # tool executions per reply (all rounds); extra calls get an error result
LLM_TOOL_DEADLINE = 90  # seconds into a reply after which no new tool round starts
MAX_TOOL_RESULT_LENGTH = 12000  # characters of JSON per tool result sent back
TOOL_ROUND_LIMIT_NOTE = (
    '已達本次回覆的商品查詢上限。請不要再呼叫工具，直接根據上面已查到的工具結果回答訪客；'
    '資料不足時請說明，並請訪客留下聯絡方式由客服人員回覆。'
)
# Always appended to the system prompt (wording from the smart home advisor
# prompt of 2026-10-30): prices, specs and stock only come from the tools.
PRODUCT_GUARD_WITH_TOOLS = (
    '【商品資料規則】\n'
    '1. 價格、規格、庫存只能使用 search_products、get_product_details 工具查到的官網資料，'
    '只引用工具回傳的數字、品名與連結；不可憑記憶、推測或自行估算，絕對不可編造價格。\n'
    '2. 訪客問到商品、價格或規格時先用工具查詢。多規格商品先說明起價，'
    '訪客選定規格後，再用 get_product_details 帶入規格值查詢該組合的實際價格。\n'
    '3. 只推薦工具查得到的已上架商品。工具回傳的品名可能是英文，'
    '回覆時先用繁體中文說明這是什麼產品，再附上工具給的 markdown 連結 [品名](網址)；'
    '只使用工具回傳的連結，不可自行組網址。\n'
    '4. 工具查不到的商品、規格或價格，以及工具沒有提供的庫存與交期，請直接說明查不到，'
    '並請訪客留下聯絡方式（姓名、電話或 Email），由客服人員報價或回覆。'
)
PRODUCT_GUARD_NO_TOOLS = (
    '【商品資料規則】\n'
    '你目前無法查詢官網的商品資料。不要回答任何價格、規格或庫存的具體內容，'
    '不可憑記憶、推測或自行估算，絕對不可編造價格；'
    '請訪客留下聯絡方式（姓名、電話或 Email），由客服人員報價或回覆。'
)

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
    '您好，我是 AI 客服助理，可以先為您解答商品與價格問題；需要真人客服請輸入「找真人」。'
)
# First block of every system prompt (P1-8).
IDENTITY_TEMPLATE = (
    '你是 {company} 的 AI 客服助理「{bot}」。被問到身分時就這樣回答；'
    '不要自稱 ChatGPT、GPT、Codex、OpenAI 或其他產品／模型名稱。'
)
DEFAULT_BOT_NAME = 'AI 客服助理'
LEGACY_BOT_NAME = 'AI Assistant'  # the 18.0.1.0.x default, renamed by the 18.0.1.1.0 migration
BOT_AVATAR_PATH = 'im_livechat_ai/static/img/ai_bot.png'
MAX_AI_MAX_TOKENS = 8192
DEFAULT_AI_MAX_TOKENS = 1024
MAX_AI_MAX_HISTORY = 200
DEFAULT_AI_MAX_HISTORY = 50
MIN_AI_TEMPERATURE = 0.0
MAX_AI_TEMPERATURE = 2.0
# Only Live Chat managers configure the AI of a channel; the ai_* fields are
# neither readable nor writable for operators, employees, portal or public.
AI_CONFIG_GROUP = 'im_livechat.im_livechat_group_manager'
BOT_CATEGORY_XMLID = 'im_livechat_ai.partner_category_ai_bot'

# Light formatting of AI replies (the tools hand out markdown links, P1-8):
# [text](https://...) and bare http(s) URLs become links, **text** bold.
# Everything else stays escaped text.
_REPLY_FORMAT_RE = re.compile(
    r'\[(?P<text>[^\]\n]{1,300})\]\((?P<url>https?://[^\s()<>"]{1,2000})\)'
    r"|(?P<bare>https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]{1,2000})"
    r'|\*\*(?P<bold>[^*\n]{1,300})\*\*'
)
_URL_TRAILING_PUNCTUATION = '.,;:!?\''
_LINK_MARKUP = Markup('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>')

# Keywords made only of these characters (English words or phrases) must
# match as whole words, case-insensitive, with any whitespace between the
# words ("live agent" does not match "olive agents"); others (e.g. Chinese)
# match anywhere in the text.
_WORD_CHARS_RE = re.compile(r"^[0-9a-z_' \-]+$")
_PAUSE_NOTICE_PLACEHOLDER_RE = re.compile(r'%\(minutes\)[sd]')


class LLMNotConfigured(Exception):
    """LiteLLM base URL or API key missing/invalid in system settings."""


class LLMInvalidResponse(ValueError):
    """The gateway answered 200 but not with the expected JSON shape."""


class LLMEmptyResponse(ValueError):
    """The model answered with no visible content."""


# Short operator-facing explanation per HTTP status (Traditional Chinese).
_HTTP_STATUS_MESSAGES = {
    400: '請求格式或模型名稱有誤',
    401: '金鑰無效或已撤銷',
    403: '金鑰沒有使用此模型的權限',
    404: '網址或模型不存在',
    408: '上游服務逾時',
    413: '對話內容太長',
    429: '呼叫太頻繁或額度已用完',
}


class ImLivechatChannel(models.Model):
    """
    Extend im_livechat.channel to add direct LLM API integration.

    This extension adds direct calls to OpenAI-compatible APIs
    (/v1/chat/completions), enabling AI-powered livechat without
    external orchestration tools.
    """
    _inherit = 'im_livechat.channel'

    # --- AI Integration Fields ---
    ai_enabled = fields.Boolean(
        string='Enable AI Integration',
        groups=AI_CONFIG_GROUP,
        default=False,
        help='Enable AI-powered automatic replies using an OpenAI-compatible API',
    )
    ai_model = fields.Selection(
        selection='_selection_ai_model',
        string='Model',
        groups=AI_CONFIG_GROUP,
        default=DEFAULT_MODEL,
        help='LiteLLM model used by this channel. The list comes from the '
             'models cached by "Fetch Available Models" in Settings > Live Chat AI.',
    )
    ai_system_prompt = fields.Text(
        string='System Prompt',
        groups=AI_CONFIG_GROUP,
        help='System prompt defining the AI assistant role and behavior',
    )
    ai_max_history = fields.Integer(
        string='Max History Messages',
        groups=AI_CONFIG_GROUP,
        default=50,
        help='Maximum number of recent messages to include as conversation context (1-200)',
    )
    ai_temperature = fields.Float(
        string='Temperature',
        groups=AI_CONFIG_GROUP,
        default=0.7,
        help='Controls randomness of LLM responses. Lower = more deterministic (0.0-2.0)',
    )
    ai_max_tokens = fields.Integer(
        string='Max Tokens',
        groups=AI_CONFIG_GROUP,
        default=1024,
        help='Maximum number of tokens in the AI response (1-8192)',
    )
    ai_max_retries = fields.Integer(
        string='Max Retries',
        groups=AI_CONFIG_GROUP,
        default=2,
        help='Retries after a failed or timed-out LLM call (0-2). Calls time out '
             'after 45 seconds; retries wait 3 seconds, then 10 seconds.',
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
    ai_product_lookup = fields.Boolean(
        string='Allow Product Lookup',
        groups=AI_CONFIG_GROUP,
        default=True,
        help='Let the AI look up published products on the website (name, website '
             'price, specifications, link) while answering. Needs the eCommerce '
             'module. Prices are never quoted from anything else.',
    )

    # --- Constraints (P1-1: checked on every create/write, AI on or off) ---

    @api.constrains('ai_max_history')
    def _check_ai_max_history(self):
        """Validate max history range."""
        for record in self:
            if record.ai_max_history < 1 or record.ai_max_history > MAX_AI_MAX_HISTORY:
                raise ValidationError(_('Max History Messages must be between 1 and 200.'))

    @api.constrains('ai_temperature')
    def _check_ai_temperature(self):
        """Validate temperature range."""
        for record in self:
            if record.ai_temperature < MIN_AI_TEMPERATURE or record.ai_temperature > MAX_AI_TEMPERATURE:
                raise ValidationError(_('Temperature must be between 0.0 and 2.0.'))

    @api.constrains('ai_max_tokens')
    def _check_ai_max_tokens(self):
        """Validate max tokens range (bounded: cost per reply)."""
        for record in self:
            if record.ai_max_tokens < 1 or record.ai_max_tokens > MAX_AI_MAX_TOKENS:
                raise ValidationError(_('Max Tokens 必須介於 1 到 %s 之間。', MAX_AI_MAX_TOKENS))

    @api.constrains('ai_max_retries')
    def _check_ai_max_retries(self):
        """Validate max retries range."""
        for record in self:
            if record.ai_max_retries < 0 or record.ai_max_retries > LLM_MAX_RETRIES:
                raise ValidationError(_('Max Retries must be between 0 and 2.'))

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

    # Re-checked when AI is enabled (values stored before these checks
    # existed, e.g. by an earlier version, must not reach the LLM).
    _AI_RANGE_CHECKS = (
        '_check_ai_max_history',
        '_check_ai_temperature',
        '_check_ai_max_tokens',
        '_check_ai_max_retries',
        '_check_ai_handoff_pause_minutes',
        '_check_ai_handoff_wait_minutes',
    )

    def _ai_check_all_ranges(self):
        for method in self._AI_RANGE_CHECKS:
            getattr(self, method)()

    @api.model
    def _ai_allowed_models(self):
        """Models a channel may choose: the cached LiteLLM list and the default."""
        return set(self._get_cached_llm_models()) | {DEFAULT_MODEL}

    @api.model
    def _ai_validate_model(self, model):
        """P1-2: a newly chosen model (or the model of a channel whose AI is
        being enabled) must be in the cached LiteLLM model list."""
        if model and model not in self._ai_allowed_models():
            raise ValidationError(_(
                '模型「%(model)s」不在可用模型清單中。請系統管理員到「設定 → Live Chat AI」'
                '按「讀取可用模型」更新清單，再從清單中選擇模型。',
                model=model,
            ))

    @api.constrains('ai_enabled', 'ai_model')
    def _check_ai_required_fields(self):
        """Validate required configuration when AI is enabled.

        Runs as the saving user (e.g. a Live Chat manager), so the system
        settings are only probed through sudo and only for presence; their
        values never reach the error message.
        """
        for record in self:
            if record.ai_enabled:
                if not record.ai_model:
                    raise UserError(_('Model is required when AI integration is enabled.'))
                if not self._llm_is_configured():
                    raise UserError(_(
                        '尚未設定 LiteLLM 連線。請系統管理員到「設定 → Live Chat AI」'
                        '填入 LiteLLM 位址與金鑰後，再啟用 AI。'
                    ))

    # --- CRUD: the bot partner exists as soon as AI is on (P2-6) ---

    @api.model
    def _check_ai_config_access(self, vals):
        """Only Live Chat managers configure the AI, and nobody but the
        module itself chooses the bot partner (clearing it is allowed: a new
        bot is created when needed).

        Field groups already stop reads and writes of the ai_* fields, but
        Odoo does not check field groups on create, so create checks here.
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

    @api.model
    def _check_ai_manager(self):
        if not self.env.is_superuser() and not self.env.user.has_group(AI_CONFIG_GROUP):
            raise AccessError(_('只有 Live Chat 管理員可以使用此功能。'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_ai_config_access(vals)
            if vals.get('ai_enabled') or vals.get('ai_model'):
                self._ai_validate_model(vals.get('ai_model', DEFAULT_MODEL))
        channels = super().create(vals_list)
        # sudo: an operator may create a channel (AI off); the ai_* fields
        # are manager-only.
        channels.sudo().filtered('ai_enabled')._get_or_create_bot_partner()
        return channels

    def write(self, vals):
        self._check_ai_config_access(vals)
        current = self.sudo()
        enabling = current.filtered(lambda c: not c.ai_enabled) if vals.get('ai_enabled') else current.browse()
        # P1-2: only a newly set/changed model, or the model of a channel
        # being enabled, is checked; an unchanged legacy value does not block
        # saving other fields.
        if vals.get('ai_model') and any(c.ai_model != vals['ai_model'] for c in current):
            self._ai_validate_model(vals['ai_model'])
        for channel in enabling:
            self._ai_validate_model(vals.get('ai_model', channel.ai_model))
        res = super().write(vals)
        if enabling:
            # P1-1: enabling AI re-validates every AI setting.
            enabling._ai_check_all_ranges()
        if 'ai_enabled' in vals or 'ai_bot_name' in vals:
            self.sudo().filtered('ai_enabled')._get_or_create_bot_partner()
        return res

    # --- Live chat window (AI disclosure, P1-7) ---

    def _get_channel_infos(self):
        """Visitors of an AI-enabled channel see the AI welcome message
        (rendered by the widget as the first message of the chat)."""
        infos = super()._get_channel_infos()
        channel = self.sudo()
        if channel.ai_enabled:
            infos['default_message'] = channel.ai_welcome_message or DEFAULT_WELCOME_MESSAGE
        return infos

    # --- Model Selection ---

    @api.model
    def _get_cached_llm_models(self):
        """Return the model ids cached by the settings button (list of str)."""
        raw = self.env['ir.config_parameter'].sudo().get_param(ICP_MODEL_CACHE)
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            _logger.warning("Ignoring invalid %s value", ICP_MODEL_CACHE)
            return []
        if not isinstance(values, list):
            return []
        return [v for v in values if isinstance(v, str) and v]

    @api.model
    def _selection_ai_model(self):
        """Selection options: cached LiteLLM models + default + values in use.

        Values already stored on channels are always included so existing
        data (e.g. a model chosen before the cache changed) stays valid.
        """
        values = set(self._get_cached_llm_models())
        values.add(DEFAULT_MODEL)
        if sql.column_exists(self.env.cr, self._table, 'ai_model'):
            self.env.cr.execute(SQL(
                "SELECT DISTINCT ai_model FROM %s WHERE ai_model IS NOT NULL AND ai_model != ''",
                SQL.identifier(self._table),
            ))
            values.update(row[0] for row in self.env.cr.fetchall())
        ordered = [DEFAULT_MODEL] + sorted(values - {DEFAULT_MODEL})
        return [(value, value) for value in ordered]

    # --- System-level LiteLLM connection ---

    @api.model
    def _get_llm_connection(self):
        """Return (base_url, api_key) from the system settings.

        Read through sudo because ir.config_parameter is restricted to
        base.group_system; callers must never expose the key.
        The base URL is normalized: no trailing slash, no /chat/completions.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        base_url = (ICP.get_param(ICP_BASE_URL) or DEFAULT_BASE_URL).strip().rstrip('/')
        if base_url.endswith('/chat/completions'):
            base_url = base_url[:-len('/chat/completions')]
        api_key = (ICP.get_param(ICP_API_KEY) or '').strip()
        return base_url, api_key

    @api.model
    def _llm_is_configured(self):
        base_url, api_key = self._get_llm_connection()
        return bool(base_url and api_key)

    @api.model
    def _llm_request(self, method, path, json_payload=None, timeout=LLM_REQUEST_TIMEOUT,
                     before_call=None):
        """Send an authenticated request to the system LiteLLM gateway.

        The URL and the key both come from ir.config_parameter (admin-only),
        so the key is never sent to a URL a non-admin could have set.
        Upstream response bodies go to the server log only.
        `before_call` runs after the last database read, right before the
        HTTP request.
        """
        base_url, api_key = self._get_llm_connection()
        if not api_key:
            raise LLMNotConfigured('LiteLLM API key is not configured')
        if not base_url.startswith(('http://', 'https://')):
            raise LLMNotConfigured('LiteLLM base URL must start with http:// or https://')
        url = base_url + path
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        }
        if before_call:
            before_call()
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=timeout)
        else:
            response = requests.post(url, json=json_payload, headers=headers, timeout=timeout)
        if response.status_code != 200:
            _logger.warning(
                "LLM API error response (HTTP %s) for %s %s: %s",
                response.status_code, method, url, (response.text or '')[:1000],
            )
        response.raise_for_status()
        return response

    @api.model
    def _fetch_llm_models(self):
        """GET {base}/models and cache the model ids in ir.config_parameter.

        Returns:
            list: sorted model ids
        """
        response = self._llm_request('GET', '/models', timeout=LLM_MODELS_TIMEOUT)
        try:
            data = response.json()
        except ValueError:
            _logger.warning("LiteLLM /models returned non-JSON: %s", (response.text or '')[:1000])
            raise LLMInvalidResponse('Invalid /models response: not JSON')
        items = data.get('data') if isinstance(data, dict) else None
        if not isinstance(items, list):
            _logger.warning("LiteLLM /models returned unexpected payload: %s", (response.text or '')[:1000])
            raise LLMInvalidResponse("Invalid /models response: no 'data' list")
        model_ids = sorted({
            item['id'] for item in items
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']
        })
        self.env['ir.config_parameter'].sudo().set_param(
            ICP_MODEL_CACHE, json.dumps(model_ids, ensure_ascii=False),
        )
        return model_ids

    @api.model
    def _describe_llm_error(self, error):
        """Map an exception to (status_code or None, short Chinese message).

        Never includes upstream response bodies, headers or the key.
        """
        if isinstance(error, requests.HTTPError):
            status = error.response.status_code if error.response is not None else None
            if status in _HTTP_STATUS_MESSAGES:
                return status, _HTTP_STATUS_MESSAGES[status]
            if status and status >= 500:
                return status, 'LiteLLM 或上游服務暫時故障'
            return status, '上游服務回應錯誤'
        if isinstance(error, requests.Timeout):
            return None, '連線逾時'
        if isinstance(error, requests.ConnectionError):
            return None, '無法連線到 LiteLLM'
        if isinstance(error, LLMNotConfigured):
            return None, '尚未設定 LiteLLM 位址或金鑰'
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

    @api.model
    def _llm_log_payload_enabled(self):
        value = self.env['ir.config_parameter'].sudo().get_param(ICP_LOG_PAYLOAD)
        return str(value).lower() in ('true', '1')

    @api.model
    def _llm_test_notification(self, model_name):
        """Send a tiny chat completion and return a display_notification action.

        Shows only the HTTP status code and a short Chinese message; the full
        error goes to the server log.
        """
        payload = {
            'model': model_name,
            'messages': [{'role': 'user', 'content': 'Connection test. Reply with "OK".'}],
            'max_tokens': 16,
        }
        try:
            response = self._llm_request('POST', '/chat/completions', json_payload=payload)
            data = response.json()
            if not isinstance(data, dict) or not data.get('choices'):
                raise LLMInvalidResponse("Invalid API response: no 'choices' field")
            status, ok = response.status_code, True
            message = _('連線成功（HTTP %(status)s），模型 %(model)s 有回應。',
                        status=status, model=model_name)
        except Exception as e:  # noqa: BLE001 - every failure becomes a short notice
            _logger.warning("LiteLLM connection test failed (model %s): %s", model_name, e, exc_info=True)
            status, short = self._describe_llm_error(e)
            ok = False
            if status:
                message = _('連線失敗（HTTP %(status)s）：%(reason)s', status=status, reason=short)
            else:
                message = _('連線失敗：%(reason)s', reason=short)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('LiteLLM 連線測試'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': not ok,
            },
        }

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

    # --- Identity (P1-8) ---

    def _ai_company_name(self):
        """Name the AI introduces itself with: the website using this
        channel, else the company."""
        self.ensure_one()
        if 'website' in self.env and 'channel_id' in self.env['website']._fields:
            website = self.env['website'].sudo().search([('channel_id', '=', self.id)], limit=1)
            if website and website.name:
                return website.name
        return self.env.company.name or ''

    def _ai_identity_block(self):
        self.ensure_one()
        return IDENTITY_TEMPLATE.format(
            company=self._ai_company_name(), bot=self.ai_bot_name or DEFAULT_BOT_NAME,
        )

    # --- Runtime bounds (P1-1: legacy or hand-edited values never reach the LLM) ---

    def _ai_max_history_value(self):
        self.ensure_one()
        return max(1, min(self.ai_max_history or DEFAULT_AI_MAX_HISTORY, MAX_AI_MAX_HISTORY))

    def _ai_max_tokens_value(self):
        self.ensure_one()
        tokens = self.ai_max_tokens
        if not tokens or tokens < 1:
            return DEFAULT_AI_MAX_TOKENS
        return min(tokens, MAX_AI_MAX_TOKENS)

    def _ai_temperature_value(self):
        self.ensure_one()
        return max(MIN_AI_TEMPERATURE, min(float(self.ai_temperature or 0.0), MAX_AI_TEMPERATURE))

    # --- Product lookup tools (package E, P1-8) ---

    def _ai_product_tools_enabled(self):
        """True when this channel lets the AI call the product tools."""
        self.ensure_one()
        return bool(self.ai_product_lookup and self.env['im_livechat.ai.tools']._tools_supported())

    def _ai_product_guard(self):
        """Fixed product rules appended to every system prompt."""
        self.ensure_one()
        return PRODUCT_GUARD_WITH_TOOLS if self._ai_product_tools_enabled() else PRODUCT_GUARD_NO_TOOLS

    def _ai_tool_definitions(self):
        """OpenAI-style tool definitions offered to the model ([] = no tools)."""
        self.ensure_one()
        if not self._ai_product_tools_enabled():
            return []
        return self.env['im_livechat.ai.tools']._tool_definitions()

    @staticmethod
    def _ai_sanitize_tool_calls(tool_calls):
        """Keep only the fields the chat completions API accepts back."""
        clean = []
        for index, call in enumerate(tool_calls or []):
            if not isinstance(call, dict):
                continue
            function = call.get('function') if isinstance(call.get('function'), dict) else {}
            clean.append({
                'id': str(call.get('id') or f'call_{index}'),
                'type': 'function',
                'function': {
                    'name': str(function.get('name') or ''),
                    'arguments': function.get('arguments') if isinstance(function.get('arguments'), str)
                    else json.dumps(function.get('arguments') or {}, ensure_ascii=False),
                },
            })
        return clean

    def _ai_run_tool_calls(self, tool_calls, max_calls=LLM_MAX_TOOL_CALLS):
        """Execute one round of tool calls; returns (the 'tool' role
        messages, number of tools executed).

        Every tool call id gets an answer (the API requires it); calls beyond
        `max_calls` (what is left of the reply's LLM_MAX_TOOL_CALLS) get an
        error result instead of running.
        """
        self.ensure_one()
        Tools = self.env['im_livechat.ai.tools']
        results = []
        executed = 0
        for call in tool_calls:
            name = call['function']['name']
            if executed >= max_calls:
                result = {'error': '本次回覆的商品查詢次數已達上限，請根據已查到的結果回答。'}
            else:
                executed += 1
                result = Tools._execute_tool(self, name, call['function']['arguments'])
            content = json.dumps(result, ensure_ascii=False, default=str)
            if len(content) > MAX_TOOL_RESULT_LENGTH:
                content = content[:MAX_TOOL_RESULT_LENGTH] + '…（結果過長已截斷）'
            results.append({'role': 'tool', 'tool_call_id': call['id'], 'content': content})
        return results, executed

    # --- Response Processing ---

    @staticmethod
    def _strip_think_tags(text):
        """
        Remove <think>...</think> reasoning blocks from LLM responses.

        Some models (e.g. MiniMax M2.7, M3, DeepSeek R1) include internal
        reasoning wrapped in <think> tags. These should not be shown to
        visitors in the chat.

        Args:
            text (str): Raw LLM response text

        Returns:
            str: Cleaned text with think blocks removed
        """
        if not text:
            return text
        # Remove <think>...</think> blocks (including multiline)
        cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        # Remove orphaned opening <think> tags (response may be cut off)
        cleaned = re.sub(r'<think>.*$', '', cleaned, flags=re.DOTALL)
        return cleaned.strip()

    # --- LLM API Call Logic ---

    def _trigger_ai_response(self, discuss_channel, message, kind='reply', run_after=None):
        """
        Queue an AI reply (or a human handoff notice) for a visitor message.

        Creates an im_livechat.ai.job in the current (visitor) transaction;
        the job is processed after commit by a background thread, or by the
        recovery cron if that thread is lost.

        Args:
            discuss_channel (discuss.channel): The livechat session
            message (mail.message): The visitor's message
            kind (str): 'reply' (LLM answer), 'handoff' (fixed notice and
                operator alert, no LLM) or 'notice' (waiting notice, no LLM)
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

        if not self.ai_model or not self._llm_is_configured():
            _logger.warning(
                "AI integration incomplete for channel %s: missing model or "
                "LiteLLM base URL/API key in Settings > Live Chat AI",
                self.name,
            )
            return Job

        return Job._create_for_message(discuss_channel, message, run_after=run_after)

    def _generate_ai_reply(self, discuss_channel, messages, before_call=None):
        """
        Produce one AI reply: tool rounds, bounded retries, a log per call.

        When the channel allows product lookup (package E), the model may ask
        for the product tools; their results are sent back and the model is
        called again. At most LLM_MAX_TOOL_CALLS tools run per reply (over at
        most LLM_MAX_TOOL_ROUNDS rounds, and no new round after
        LLM_TOOL_DEADLINE seconds); the next call is then sent with
        tool_choice "none" and a note to answer with what it has. A response
        without tool calls is the final answer.

        Failed calls are retried at most ai_max_retries times (0-2) per reply,
        waiting LLM_RETRY_BACKOFF seconds (3 s, then 10 s); each call times out
        after LLM_REQUEST_TIMEOUT (45 s).

        Args:
            discuss_channel (discuss.channel): The chat session (for logs)
            messages (list): Chat completion messages
            before_call (callable|None): Called before each HTTP call
                (the job commits there so no transaction stays open)

        Returns:
            dict: reply (str or None), error (short str or None), calls (int),
                tool_rounds (int)
        """
        self.ensure_one()
        tools = self._ai_tool_definitions()
        conversation = list(messages)
        budget = {
            'retries': max(0, min(self.ai_max_retries, LLM_MAX_RETRIES)),
            'used': 0,
            'calls': 0,
        }
        started = time.time()
        rounds = 0
        tools_run = 0

        while True:
            final = bool(tools) and (
                rounds >= LLM_MAX_TOOL_ROUNDS
                or tools_run >= LLM_MAX_TOOL_CALLS
                or time.time() - started > LLM_TOOL_DEADLINE
            )
            # Each call gets its own copy of the history (it grows per round).
            if not tools:
                data, error = self._llm_call_with_retries(
                    discuss_channel, list(conversation), budget, before_call)
            elif final:
                data, error = self._llm_call_with_retries(
                    discuss_channel, conversation + [{'role': 'system', 'content': TOOL_ROUND_LIMIT_NOTE}],
                    budget, before_call, tools=tools, tool_choice='none')
            else:
                data, error = self._llm_call_with_retries(
                    discuss_channel, list(conversation), budget, before_call,
                    tools=tools, tool_choice='auto', allow_tool_calls=True)
            if error:
                return {'reply': None, 'error': error, 'calls': budget['calls'], 'tool_rounds': rounds}

            message = data['choices'][0].get('message') or {}
            tool_calls = self._ai_sanitize_tool_calls(message.get('tool_calls')) if tools and not final else []
            if not tool_calls:
                reply = self._strip_think_tags(message.get('content') or '')
                return {'reply': reply, 'error': None, 'calls': budget['calls'], 'tool_rounds': rounds}

            rounds += 1
            _logger.info(
                "AI reply for channel %s: tool round %s/%s (%s)", self.id, rounds,
                LLM_MAX_TOOL_ROUNDS, ', '.join(c['function']['name'] for c in tool_calls),
            )
            conversation.append({
                'role': 'assistant',
                'content': message.get('content') or None,
                'tool_calls': tool_calls,
            })
            results, executed = self._ai_run_tool_calls(tool_calls, LLM_MAX_TOOL_CALLS - tools_run)
            tools_run += executed
            conversation.extend(results)

    def _llm_call_with_retries(self, discuss_channel, messages, budget, before_call=None,
                               tools=None, tool_choice=None, allow_tool_calls=False):
        """
        One chat completion step, retried while the reply's retry budget lasts.

        A response is valid when it has visible content (after removing
        <think> blocks) or, if allow_tool_calls, tool calls. Every call is
        logged (tokens, time, requested tool names; content only when
        "Log Conversation Content" is on).

        Returns:
            tuple: (response dict, None) on success, (None, short error) when
                every call failed
        """
        self.ensure_one()
        env = self.env
        attempts = 0
        last_error = None
        while True:
            if attempts:
                if before_call:
                    before_call()  # no transaction stays open during the wait
                wait = LLM_RETRY_BACKOFF[min(budget['used'] - 1, len(LLM_RETRY_BACKOFF) - 1)]
                time.sleep(wait)
            attempts += 1
            budget['calls'] += 1
            start_time = time.time()
            try:
                # before_call runs inside, after the last database read.
                response_data = self._call_llm_api(
                    messages, tools=tools, tool_choice=tool_choice, before_call=before_call)
                response_time = time.time() - start_time

                message = response_data.get('choices', [{}])[0].get('message') or {}
                tool_names = []
                if allow_tool_calls and isinstance(message.get('tool_calls'), list):
                    tool_names = [
                        (call.get('function') or {}).get('name') or '?'
                        for call in message['tool_calls'] if isinstance(call, dict)
                    ]
                if not tool_names and not self._strip_think_tags(message.get('content') or ''):
                    raise LLMEmptyResponse("Empty response from LLM API")

                usage = response_data.get('usage') or {}
                self._create_api_log(
                    env,
                    channel_id=self.id,
                    discuss_channel_id=discuss_channel.id,
                    model_name=self.ai_model,
                    status='success',
                    request_payload=messages,
                    response_payload=response_data,
                    prompt_tokens=usage.get('prompt_tokens', 0),
                    completion_tokens=usage.get('completion_tokens', 0),
                    total_tokens=usage.get('total_tokens', 0),
                    response_time=response_time,
                    retry_count=attempts - 1,
                    tool_names=', '.join(tool_names),
                )
                return response_data, None

            except Exception as e:  # noqa: BLE001 - every failure is retried or reported
                response_time = time.time() - start_time
                # Full detail goes to the server log only; the stored log
                # keeps a short message without the upstream response body.
                last_error = self._short_llm_error(e)
                can_retry = budget['used'] < budget['retries']
                _logger.warning(
                    "AI API call failed for channel %s (call %s of this step, %s retries left): %s",
                    self.id, attempts, budget['retries'] - budget['used'], e,
                )
                if can_retry:
                    budget['used'] += 1
                    self._create_api_log(
                        env,
                        channel_id=self.id,
                        discuss_channel_id=discuss_channel.id,
                        model_name=self.ai_model,
                        status='retry',
                        request_payload=messages,
                        response_payload=None,
                        response_time=response_time,
                        error_message=last_error,
                        retry_count=attempts,
                    )
                    continue

                self._create_api_log(
                    env,
                    channel_id=self.id,
                    discuss_channel_id=discuss_channel.id,
                    model_name=self.ai_model,
                    status='error',
                    request_payload=messages,
                    response_payload=None,
                    response_time=None,
                    error_message=f'All {attempts} calls failed. Last error: {last_error}'[:MAX_LOG_ERROR_LENGTH],
                    retry_count=attempts - 1,
                )
                return None, last_error

    def _call_llm_api(self, messages, tools=None, tool_choice=None, before_call=None):
        """
        Call the system LiteLLM gateway (OpenAI-compatible chat completions).

        The base URL and API key come from Settings > Live Chat AI
        (ir.config_parameter, admin-only); the channel only chooses the model.

        Args:
            messages (list): List of message dicts with 'role' and 'content' keys
            tools (list|None): OpenAI-style tool definitions (package E)
            tool_choice (str|None): 'auto' or 'none' (sent only with tools)
            before_call (callable|None): called after the payload and the
                connection were read and right before the HTTP request (the
                job commits there, so no transaction is open during the call)

        Returns:
            dict: The API response JSON

        Raises:
            requests.RequestException: On HTTP errors
            LLMNotConfigured: When the system settings are incomplete
            ValueError: On invalid response format
        """
        self.ensure_one()

        payload = {
            'model': self.ai_model,
            'messages': messages,
        }

        # Clamped (P1-1): values stored before the range checks existed are
        # never sent as they are.
        payload['temperature'] = self._ai_temperature_value()
        payload['max_tokens'] = self._ai_max_tokens_value()
        if tools:
            payload['tools'] = tools
            if tool_choice:
                payload['tool_choice'] = tool_choice

        response = self._llm_request('POST', '/chat/completions', json_payload=payload,
                                     before_call=before_call)

        data = response.json()
        if not isinstance(data, dict) or not data.get('choices'):
            _logger.warning("LLM API response without 'choices': %s", (response.text or '')[:1000])
            raise LLMInvalidResponse("Invalid API response: no 'choices' field")

        return data

    def _create_bot_message(self, env, discuss_channel, body):
        """
        Post a message to the livechat session as the AI bot.

        The body is escaped to prevent XSS from LLM responses; markdown
        links and bare http(s) URLs become links, **text** becomes bold and
        newlines are converted to <br/> for display.

        Args:
            env (api.Environment): Environment with new cursor
            discuss_channel (discuss.channel): The chat session
            body (str): Message body text

        Returns:
            mail.message: the posted message
        """
        self.ensure_one()

        bot_partner = self._get_bot_partner()

        # Escape HTML to prevent XSS, then convert newlines for display.
        # Only links to the store's own hosts become clickable.
        hosts = self._ai_link_hosts()
        safe_body = Markup('<br/>').join(
            self._format_reply_line(line, hosts) for line in body.split('\n')
        )

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
        """Hosts AI replies may link to: web.base.url (the product tools build
        their URLs from it) and the domains of the websites."""
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

    @classmethod
    def _format_reply_line(cls, line, allowed_hosts=()):
        """One line of AI text -> safe Markup: escaped text, with markdown
        links / bare http(s) URLs as links and **bold**. Only http(s) URLs
        whose host is in `allowed_hosts` (the store's own hosts) become
        links: an off-site markdown link keeps only its text and an off-site
        bare URL stays plain text, so a prompt-injected reply cannot post a
        clickable phishing link. Every piece of text and every URL is escaped."""
        parts = []
        position = 0
        for match in _REPLY_FORMAT_RE.finditer(line):
            parts.append(Markup.escape(line[position:match.start()]))
            if match.group('url'):
                if cls._url_host(match.group('url')) in allowed_hosts:
                    parts.append(_LINK_MARKUP % (match.group('url'), match.group('text')))
                else:
                    parts.append(Markup.escape(match.group('text')))
            elif match.group('bare'):
                url = match.group('bare')
                trailing = len(url) - len(url.rstrip(_URL_TRAILING_PUNCTUATION))
                url, tail = (url[:-trailing], url[-trailing:]) if trailing else (url, '')
                if cls._url_host(url) in allowed_hosts:
                    parts.append(_LINK_MARKUP % (url, url))
                else:
                    parts.append(Markup.escape(url))
                parts.append(Markup.escape(tail))
            else:
                parts.append(Markup('<strong>%s</strong>') % match.group('bold'))
            position = match.end()
        parts.append(Markup.escape(line[position:]))
        return Markup('').join(parts)

    def _create_api_log(self, env, channel_id, discuss_channel_id, model_name,
                        status, request_payload, response_payload,
                        prompt_tokens=0, completion_tokens=0, total_tokens=0,
                        response_time=None, error_message=None, retry_count=0,
                        tool_names=None):
        """
        Create an LLM API log entry.

        Uses the provided environment (which should have its own cursor)
        to ensure logs are persisted independently of the main transaction.

        Args:
            env (api.Environment): Environment to use for creation
            channel_id (int): Livechat channel ID
            discuss_channel_id (int): Discuss channel ID
            model_name (str): LLM model name
            status (str): 'success', 'error', or 'retry'
            request_payload (list|None): Messages array sent to API
            response_payload (dict|None): API response data
            prompt_tokens (int): Number of prompt tokens
            completion_tokens (int): Number of completion tokens
            total_tokens (int): Total tokens used
            response_time (float|None): Response time in seconds
            error_message (str|None): Error message if failed
            retry_count (int): Number of retry attempt
            tool_names (str|None): Tools the model asked for in this response
        """
        try:
            vals = {
                'livechat_channel_id': channel_id,
                'model': model_name,
                'status': status,
                'response_time': response_time,
                'error_message': (error_message or '')[:MAX_LOG_ERROR_LENGTH] or False,
                'retry_count': retry_count,
                'prompt_tokens': prompt_tokens or 0,
                'completion_tokens': completion_tokens or 0,
                'total_tokens': total_tokens or 0,
                'tool_names': tool_names or False,
            }

            # Validate FK references
            if discuss_channel_id and env['discuss.channel'].browse(discuss_channel_id).exists():
                vals['discuss_channel_id'] = discuss_channel_id

            # Conversation content is only kept when the admin turned on
            # "Log Conversation Content" (default off, P2-1).
            if self._llm_log_payload_enabled():
                if request_payload:
                    vals['request_payload'] = json.dumps(request_payload, ensure_ascii=False, default=str)
                if response_payload:
                    vals['response_payload'] = json.dumps(response_payload, ensure_ascii=False, default=str)

            env['llm.api.log'].create(vals)
        except Exception as log_error:
            _logger.error("Failed to create API log: %s", log_error, exc_info=True)

    # --- UI Action Methods ---

    def action_test_ai_connection(self):
        """
        Test the LiteLLM connection with this channel's model.

        Returns a notification with the HTTP status code and a short
        Chinese message only; details go to the server log. Live Chat
        managers only: the call uses the company LiteLLM key.
        """
        self.ensure_one()
        self._check_ai_manager()

        if not self.ai_enabled:
            raise UserError(_('AI integration is not enabled for this channel.'))

        if not self.ai_model:
            raise UserError(_('Please choose a model before testing.'))

        return self._llm_test_notification(self.ai_model)

    def action_view_api_logs(self):
        """Open the API logs view filtered for this channel."""
        self.ensure_one()
        self._check_ai_manager()

        return {
            'type': 'ir.actions.act_window',
            'name': _('API Logs - %s') % self.name,
            'res_model': 'llm.api.log',
            'view_mode': 'list,form',
            'domain': [('livechat_channel_id', '=', self.id)],
            'context': {
                'default_livechat_channel_id': self.id,
                'search_default_filter_last_7_days': 1,
            },
        }
