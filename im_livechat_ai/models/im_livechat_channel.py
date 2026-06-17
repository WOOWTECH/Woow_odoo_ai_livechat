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

_logger = logging.getLogger(__name__)


class ImLivechatChannel(models.Model):
    """
    Extend im_livechat.channel to add direct LLM API integration.

    This extension adds direct calls to OpenAI-compatible APIs
    (/v1/chat/completions), enabling AI-powered livechat without
    external orchestration tools.
    """
    _inherit = 'im_livechat.channel'

    # --- WELL Building Standard Mode ---
    ai_well_mode = fields.Boolean(
        string='WELL Building Assistant Mode',
        default=False,
        help='When enabled, prepends WELL Building Standard knowledge to the system prompt '
             'to help occupants with indoor air quality, thermal comfort, lighting, noise, '
             'water quality, and wellness program questions.',
    )

    # --- AI Integration Fields ---
    ai_enabled = fields.Boolean(
        string='Enable AI Integration',
        default=False,
        help='Enable AI-powered automatic replies using an OpenAI-compatible API',
    )
    ai_api_base_url = fields.Char(
        string='API Base URL',
        help='Base URL of the OpenAI-compatible API (e.g., https://api.openai.com/v1)',
    )
    ai_api_key = fields.Char(
        string='API Key',
        help='API key for authenticating with the LLM service',
        groups='base.group_system',
        copy=False,
    )
    ai_model = fields.Char(
        string='Model',
        help='Model name to use (e.g., gpt-4o, deepseek-chat, MiniMax-Text-01)',
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

    WELL_SYSTEM_PROMPT = (
        "You are a WELL Building Standard assistant. Help occupants with questions "
        "about indoor air quality, thermal comfort, lighting, noise levels, water quality, "
        "and wellness programs. Guide them to report issues through the maintenance portal "
        "or book wellness spaces."
    )

    def _get_effective_system_prompt(self):
        """Return the system prompt, optionally prepended with WELL knowledge."""
        self.ensure_one()
        base_prompt = self.ai_system_prompt or ''
        if self.ai_well_mode:
            if base_prompt:
                return f"{self.WELL_SYSTEM_PROMPT}\n\n{base_prompt}"
            return self.WELL_SYSTEM_PROMPT
        return base_prompt

    # --- Constraints ---

    @api.constrains('ai_api_base_url')
    def _check_ai_api_base_url(self):
        """Validate API base URL format."""
        for record in self:
            if record.ai_api_base_url:
                url = record.ai_api_base_url.strip()
                if not url.startswith(('http://', 'https://')):
                    raise UserError(_('API Base URL must start with http:// or https://'))

    @api.constrains('ai_max_history')
    def _check_ai_max_history(self):
        """Validate max history range."""
        for record in self:
            if record.ai_enabled:
                if record.ai_max_history < 0 or record.ai_max_history > 200:
                    raise ValidationError(_('Max History Messages must be between 0 and 200.'))

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

    @api.constrains('ai_enabled', 'ai_api_base_url', 'ai_api_key', 'ai_model')
    def _check_ai_required_fields(self):
        """Validate required fields when AI is enabled."""
        for record in self:
            if record.ai_enabled:
                if not record.ai_api_base_url:
                    raise UserError(_('API Base URL is required when AI integration is enabled.'))
                if not record.ai_api_key:
                    raise UserError(_('API Key is required when AI integration is enabled.'))
                if not record.ai_model:
                    raise UserError(_('Model is required when AI integration is enabled.'))

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

        if not self.ai_api_base_url or not self.ai_api_key or not self.ai_model:
            _logger.warning(
                "AI integration incomplete for channel %s: missing API URL, key, or model",
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
                    raise ValueError("Empty response from LLM API")

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
                last_error = str(e)
                # For HTTP errors, include response body for debugging
                if isinstance(e, requests.HTTPError) and e.response is not None:
                    last_error = f"{e} | Response: {e.response.text[:500]}"
                _logger.warning(
                    "AI API call failed for channel %s (attempt %s/%s): %s",
                    channel_id, attempt + 1, max_retries, last_error,
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
            error_message=f'All {max_retries} retries exhausted. Last error: {last_error}',
            retry_count=max_retries,
        )

    def _call_llm_api(self, messages):
        """
        Call an OpenAI-compatible chat completions API.

        Args:
            messages (list): List of message dicts with 'role' and 'content' keys

        Returns:
            dict: The API response JSON

        Raises:
            requests.RequestException: On HTTP errors
            ValueError: On invalid response format
        """
        self.ensure_one()

        url = self.ai_api_base_url.strip().rstrip('/')
        if '/chat/completions' not in url:
            url = url + '/chat/completions'

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.ai_api_key}',
        }

        payload = {
            'model': self.ai_model,
            'messages': messages,
        }

        if self.ai_temperature is not None:
            payload['temperature'] = self.ai_temperature
        if self.ai_max_tokens:
            payload['max_tokens'] = self.ai_max_tokens

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=180,
        )
        if response.status_code != 200:
            _logger.warning(
                "LLM API error response (HTTP %s): %s",
                response.status_code, response.text[:1000],
            )
        response.raise_for_status()

        data = response.json()
        if not data.get('choices'):
            raise ValueError(f"Invalid API response: no 'choices' field. Response: {response.text[:500]}")

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
                'error_message': error_message,
                'retry_count': retry_count,
                'prompt_tokens': prompt_tokens or 0,
                'completion_tokens': completion_tokens or 0,
                'total_tokens': total_tokens or 0,
            }

            # Validate FK references
            if discuss_channel_id and env['discuss.channel'].browse(discuss_channel_id).exists():
                vals['discuss_channel_id'] = discuss_channel_id

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
        Test the AI API connection by sending a simple request.

        Returns a user notification indicating success or failure.
        """
        self.ensure_one()

        if not self.ai_enabled:
            raise UserError(_('AI integration is not enabled for this channel.'))

        if not self.ai_api_base_url or not self.ai_api_key or not self.ai_model:
            raise UserError(_('Please configure API Base URL, API Key, and Model before testing.'))

        test_messages = [
            {'role': 'user', 'content': 'Hello, this is a connection test. Reply with "OK".'}
        ]

        try:
            response_data = self._call_llm_api(test_messages)
            raw_reply = response_data.get('choices', [{}])[0].get('message', {}).get('content', '')
            reply = self._strip_think_tags(raw_reply)
            usage = response_data.get('usage', {})

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('AI Connection Test Successful'),
                    'message': _('Model: %s | Reply: %s | Tokens: %s') % (
                        self.ai_model,
                        reply[:100],
                        usage.get('total_tokens', 'N/A'),
                    ),
                    'type': 'success',
                    'sticky': False,
                },
            }

        except requests.exceptions.Timeout:
            raise UserError(_('API request timed out. Please check the URL and try again.'))
        except requests.exceptions.ConnectionError:
            raise UserError(_('Could not connect to API. Please check the URL and network connection.'))
        except Exception as e:
            raise UserError(_('AI connection test failed: %s') % str(e))

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
