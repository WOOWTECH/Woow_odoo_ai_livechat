# -*- coding: utf-8 -*-

import json
import logging
import re
import threading
import time

import psycopg2
import requests
from markupsafe import Markup

from odoo import api, fields, models, _, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError
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

LLM_REQUEST_TIMEOUT = 180  # seconds, chat completions
LLM_MODELS_TIMEOUT = 15  # seconds, GET /models
MAX_LOG_ERROR_LENGTH = 300

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
        default=False,
        help='Enable AI-powered automatic replies using an OpenAI-compatible API',
    )
    ai_model = fields.Selection(
        selection='_selection_ai_model',
        string='Model',
        default=DEFAULT_MODEL,
        help='LiteLLM model used by this channel. The list comes from the '
             'models cached by "Fetch Available Models" in Settings > Live Chat AI.',
    )
    ai_system_prompt = fields.Text(
        string='System Prompt',
        help='System prompt defining the AI assistant role and behavior',
    )
    ai_max_history = fields.Integer(
        string='Max History Messages',
        default=50,
        help='Maximum number of recent messages to include as conversation context (1-200)',
    )
    ai_temperature = fields.Float(
        string='Temperature',
        default=0.7,
        help='Controls randomness of LLM responses. Lower = more deterministic (0.0-2.0)',
    )
    ai_max_tokens = fields.Integer(
        string='Max Tokens',
        default=1024,
        help='Maximum number of tokens in the AI response',
    )
    ai_max_retries = fields.Integer(
        string='Max Retries',
        default=3,
        help='Maximum number of retry attempts for failed API calls (1-10)',
    )
    ai_retry_delay = fields.Integer(
        string='Retry Delay (seconds)',
        default=2,
        help='Delay between retry attempts in seconds (1-30)',
    )
    ai_error_message = fields.Text(
        string='Error Message',
        default='Sorry, the AI assistant is temporarily unavailable. Please try again later or leave your contact information.',
        help='Message sent to visitor when the AI API call fails after all retries',
    )
    ai_bot_partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Bot Partner',
        ondelete='set null',
        help='The partner record used as the author for AI-generated messages',
    )
    ai_bot_name = fields.Char(
        string='Bot Name',
        default='AI Assistant',
        help='Display name for the AI bot in chat',
    )

    # --- Constraints ---

    @api.constrains('ai_max_history')
    def _check_ai_max_history(self):
        """Validate max history range."""
        for record in self:
            if record.ai_enabled:
                if record.ai_max_history < 1 or record.ai_max_history > 200:
                    raise ValidationError(_('Max History Messages must be between 1 and 200.'))

    @api.constrains('ai_temperature')
    def _check_ai_temperature(self):
        """Validate temperature range."""
        for record in self:
            if record.ai_enabled:
                if record.ai_temperature < 0.0 or record.ai_temperature > 2.0:
                    raise ValidationError(_('Temperature must be between 0.0 and 2.0.'))

    @api.constrains('ai_max_tokens')
    def _check_ai_max_tokens(self):
        """Validate max tokens is a positive integer."""
        for record in self:
            if record.ai_enabled:
                if record.ai_max_tokens < 1:
                    raise ValidationError(_('Max Tokens must be at least 1.'))

    @api.constrains('ai_max_retries')
    def _check_ai_max_retries(self):
        """Validate max retries range."""
        for record in self:
            if record.ai_enabled:
                if record.ai_max_retries < 1 or record.ai_max_retries > 10:
                    raise ValidationError(_('Max Retries must be between 1 and 10.'))

    @api.constrains('ai_retry_delay')
    def _check_ai_retry_delay(self):
        """Validate retry delay range."""
        for record in self:
            if record.ai_enabled:
                if record.ai_retry_delay < 1 or record.ai_retry_delay > 30:
                    raise ValidationError(_('Retry Delay must be between 1 and 30 seconds.'))

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
    def _llm_request(self, method, path, json_payload=None, timeout=LLM_REQUEST_TIMEOUT):
        """Send an authenticated request to the system LiteLLM gateway.

        The URL and the key both come from ir.config_parameter (admin-only),
        so the key is never sent to a URL a non-admin could have set.
        Upstream response bodies go to the server log only.
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

    def _get_or_create_bot_partner(self):
        """
        Get or create the bot partner for this channel.

        If ai_bot_partner_id is already set, updates the name if changed.
        Otherwise creates a new res.partner and links it to the channel.

        Returns:
            res.partner: The bot partner record
        """
        self.ensure_one()
        bot_name = self.ai_bot_name or 'AI Assistant'

        if self.ai_bot_partner_id:
            if self.ai_bot_partner_id.name != bot_name:
                self.ai_bot_partner_id.sudo().write({'name': bot_name})
            return self.ai_bot_partner_id

        # Create new bot partner
        partner = self.env['res.partner'].sudo().create({
            'name': bot_name,
            'active': True,
            'is_company': False,
            'type': 'contact',
        })
        self.write({'ai_bot_partner_id': partner.id})
        return partner

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

    def _trigger_ai_response(self, discuss_channel, message):
        """
        Trigger AI response asynchronously in a separate thread.

        This method is non-blocking to avoid slowing down chat operations.

        Args:
            discuss_channel (discuss.channel): The livechat session
            message (mail.message): The visitor's message
        """
        self.ensure_one()

        if not self.ai_enabled:
            return

        if not self.ai_model or not self._llm_is_configured():
            _logger.warning(
                "AI integration incomplete for channel %s: missing model or "
                "LiteLLM base URL/API key in Settings > Live Chat AI",
                self.name,
            )
            return

        # Capture IDs and config for thread context
        channel_id = self.id
        discuss_channel_id = discuss_channel.id
        db_name = self.env.cr.dbname

        thread = threading.Thread(
            target=self._process_ai_response,
            args=(db_name, channel_id, discuss_channel_id),
            daemon=True,
        )
        thread.start()

    def _process_ai_response(self, db_name, channel_id, discuss_channel_id, test_env=None):
        """
        Process AI response in a background thread.

        Opens a new database cursor, builds conversation context,
        calls the LLM API with retry logic, and posts the response.

        Args:
            db_name (str): Database name
            channel_id (int): ID of the im_livechat.channel
            discuss_channel_id (int): ID of the discuss.channel session
            test_env (api.Environment|None): If provided, use this environment
                instead of creating a new cursor. Used for testing only.
        """
        if test_env is not None:
            self._do_process_ai_response(test_env, channel_id, discuss_channel_id)
            return

        max_commit_retries = 5
        for commit_attempt in range(max_commit_retries):
            try:
                with self.pool.cursor() as cr:
                    # Use READ COMMITTED to avoid serialization conflicts with
                    # the main thread's message_post updating last_interest_dt
                    cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    self._do_process_ai_response(env, channel_id, discuss_channel_id)
                    cr.commit()
                return  # Success
            except psycopg2.errors.SerializationFailure:
                if commit_attempt < max_commit_retries - 1:
                    backoff = 1.0 * (2 ** commit_attempt)  # 1s, 2s, 4s, 8s
                    _logger.warning(
                        "Serialization failure in AI thread (attempt %d/%d), "
                        "retrying in %.1fs...",
                        commit_attempt + 1, max_commit_retries, backoff,
                    )
                    time.sleep(backoff)
                else:
                    _logger.error("Serialization failure in AI thread after %d attempts", max_commit_retries)
            except Exception as e:
                _logger.error("Unexpected error in AI response thread: %s", e, exc_info=True)
                return

    def _do_process_ai_response(self, env, channel_id, discuss_channel_id):
        """
        Core logic for processing an AI response.

        Separated from _process_ai_response to allow testing without
        a separate database cursor.

        Args:
            env (api.Environment): Environment to use
            channel_id (int): ID of the im_livechat.channel
            discuss_channel_id (int): ID of the discuss.channel session
        """
        channel = env['im_livechat.channel'].browse(channel_id)
        if not channel.exists():
            _logger.warning("AI response: channel %s no longer exists", channel_id)
            return

        discuss_channel = env['discuss.channel'].browse(discuss_channel_id)
        if not discuss_channel.exists():
            _logger.warning("AI response: session %s no longer exists", discuss_channel_id)
            return

        # Build conversation messages (with retry if visitor msg not yet committed)
        messages = discuss_channel._build_llm_messages(channel)
        user_messages = [m for m in messages if m.get('role') == 'user']
        if not user_messages:
            # Visitor message may not be committed yet — wait and retry
            for wait_attempt in range(5):
                time.sleep(0.5 * (wait_attempt + 1))
                env.cr.rollback()  # Reset transaction to see new commits
                discuss_channel = env['discuss.channel'].browse(discuss_channel_id)
                messages = discuss_channel._build_llm_messages(channel)
                user_messages = [m for m in messages if m.get('role') == 'user']
                if user_messages:
                    break
            if not user_messages:
                _logger.warning(
                    "AI response: no user messages found in channel %s after retries",
                    discuss_channel_id,
                )
                return

        # Read config values
        max_retries = max(1, min(channel.ai_max_retries or 3, 10))
        retry_delay = max(1, min(channel.ai_retry_delay or 2, 30))

        last_error = None

        for attempt in range(max_retries):
            start_time = time.time()
            try:
                response_data = channel._call_llm_api(messages)
                response_time = time.time() - start_time

                # Extract response content and strip reasoning tags
                raw_reply = response_data.get('choices', [{}])[0].get('message', {}).get('content', '')
                ai_reply = self._strip_think_tags(raw_reply)
                if not ai_reply:
                    raise LLMEmptyResponse("Empty response from LLM API")

                # Extract token usage
                usage = response_data.get('usage', {})

                # Post AI reply to chat
                channel._create_bot_message(env, discuss_channel, ai_reply)

                # Log success
                channel._create_api_log(
                    env,
                    channel_id=channel_id,
                    discuss_channel_id=discuss_channel_id,
                    model_name=channel.ai_model,
                    status='success',
                    request_payload=messages,
                    response_payload=response_data,
                    prompt_tokens=usage.get('prompt_tokens', 0),
                    completion_tokens=usage.get('completion_tokens', 0),
                    total_tokens=usage.get('total_tokens', 0),
                    response_time=response_time,
                    retry_count=attempt,
                )
                return

            except Exception as e:
                response_time = time.time() - start_time
                # Full detail goes to the server log only; the stored log
                # keeps a short message without the upstream response body.
                last_error = channel._short_llm_error(e)
                _logger.warning(
                    "AI API call failed for channel %s (attempt %s/%s): %s",
                    channel_id, attempt + 1, max_retries, e,
                )

                # Log retry attempt
                if attempt < max_retries - 1:
                    channel._create_api_log(
                        env,
                        channel_id=channel_id,
                        discuss_channel_id=discuss_channel_id,
                        model_name=channel.ai_model,
                        status='retry',
                        request_payload=messages,
                        response_payload=None,
                        response_time=response_time,
                        error_message=last_error,
                        retry_count=attempt + 1,
                    )
                    time.sleep(retry_delay)

        # All retries exhausted — send error message to visitor
        error_msg = channel.ai_error_message or _(
            'Sorry, the AI assistant is temporarily unavailable. '
            'Please try again later or leave your contact information.'
        )
        channel._create_bot_message(env, discuss_channel, error_msg)

        # Log final failure
        channel._create_api_log(
            env,
            channel_id=channel_id,
            discuss_channel_id=discuss_channel_id,
            model_name=channel.ai_model,
            status='error',
            request_payload=messages,
            response_payload=None,
            response_time=None,
            error_message=f'All {max_retries} retries exhausted. Last error: {last_error}'[:MAX_LOG_ERROR_LENGTH],
            retry_count=max_retries,
        )

    def _call_llm_api(self, messages):
        """
        Call the system LiteLLM gateway (OpenAI-compatible chat completions).

        The base URL and API key come from Settings > Live Chat AI
        (ir.config_parameter, admin-only); the channel only chooses the model.

        Args:
            messages (list): List of message dicts with 'role' and 'content' keys

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

        if self.ai_temperature is not None:
            payload['temperature'] = self.ai_temperature
        if self.ai_max_tokens:
            payload['max_tokens'] = self.ai_max_tokens

        response = self._llm_request('POST', '/chat/completions', json_payload=payload)

        data = response.json()
        if not isinstance(data, dict) or not data.get('choices'):
            _logger.warning("LLM API response without 'choices': %s", (response.text or '')[:1000])
            raise LLMInvalidResponse("Invalid API response: no 'choices' field")

        return data

    def _create_bot_message(self, env, discuss_channel, body):
        """
        Post a message to the livechat session as the AI bot.

        The body is escaped to prevent XSS from LLM responses,
        then newlines are converted to <br/> for display.

        Args:
            env (api.Environment): Environment with new cursor
            discuss_channel (discuss.channel): The chat session
            body (str): Message body text
        """
        self.ensure_one()

        bot_partner = self._get_or_create_bot_partner()

        # Escape HTML to prevent XSS, then convert newlines for display
        safe_body = Markup('<br/>').join(
            Markup.escape(line) for line in body.split('\n')
        )

        discuss_channel.with_context(mail_create_nosubscribe=True).message_post(
            body=safe_body,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=bot_partner.id,
        )

    def _create_api_log(self, env, channel_id, discuss_channel_id, model_name,
                        status, request_payload, response_payload,
                        prompt_tokens=0, completion_tokens=0, total_tokens=0,
                        response_time=None, error_message=None, retry_count=0):
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
        Chinese message only; details go to the server log.
        """
        self.ensure_one()

        if not self.ai_enabled:
            raise UserError(_('AI integration is not enabled for this channel.'))

        if not self.ai_model:
            raise UserError(_('Please choose a model before testing.'))

        return self._llm_test_notification(self.ai_model)

    def action_view_api_logs(self):
        """Open the API logs view filtered for this channel."""
        self.ensure_one()

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
