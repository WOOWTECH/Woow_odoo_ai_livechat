# -*- coding: utf-8 -*-
"""Shared fixtures for im_livechat_ai tests (no real HTTP, no real key).

18.0.2.0.0: a channel's AI is an AI assistant (``im_livechat.ai.assistant``)
that uses an AI connection (``im_livechat.ai.connection``). The helpers accept
the 18.0.1.x channel field names (``ai_backend_type``, ``ai_endpoint_url``,
``ai_api_token``, ``ai_model``, ``ai_system_prompt`` ...) and put each value
where it lives now, so the tests of behaviour that did not change keep their
setup and assertions.
"""

from unittest.mock import MagicMock

from odoo.addons.im_livechat_ai.models.ai_connection import LITELLM_DEFAULT_TIMEOUT, N8N_DEFAULT_TIMEOUT
from odoo.addons.im_livechat_ai.models.im_livechat_channel import HANDOFF_KEYWORDS_1_1_0

TEST_BASE_URL = 'https://litellm.test/v1'
TEST_API_KEY = 'dummy-test-token'
TEST_MODEL = 'chatgpt-pool/gpt-6-sol'
# Backend connection of an AI-enabled test channel (LiteLLM), in 18.0.1.x
# field names (see make_ai_channel). The handoff keywords (an optional
# pre-filter, empty on a new channel since owner decision 40) are configured
# with the 18.0.1.1.0 list, so the keyword tests keep their meaning.
TEST_HANDOFF_KEYWORDS = HANDOFF_KEYWORDS_1_1_0
TEST_BACKEND = {
    'ai_backend_type': 'openai',
    'ai_endpoint_url': TEST_BASE_URL,
    'ai_api_token': TEST_API_KEY,
    'ai_model': TEST_MODEL,
    'ai_handoff_keywords': TEST_HANDOFF_KEYWORDS,
}
TEST_N8N_URL = 'https://n8n.test/webhook/livechat-ai'
TEST_N8N_SECRET = 'dummy-signing-secret'
TEST_N8N_BACKEND = {
    'ai_backend_type': 'n8n',
    'ai_endpoint_url': TEST_N8N_URL,
    'ai_api_token': TEST_N8N_SECRET,
}

# 18.0.1.x channel field -> field of the AI connection / AI assistant
CONNECTION_FIELDS = {
    'ai_backend_type': 'backend_type',
    'ai_endpoint_url': 'endpoint_url',
    'ai_api_token': 'api_key',
    'ai_model': 'model',
    'ai_document_mode': 'document_mode',
    'ai_max_retries': 'max_retries',
    'ai_request_timeout': 'timeout_seconds',
    'ai_log_payload': 'log_payload',
}
ASSISTANT_FIELDS = {
    'ai_system_prompt': 'system_prompt',
    'ai_max_history': 'max_history',
    'ai_error_message': 'error_message',
    'ai_bot_name': 'name',
}


def split_ai_vals(vals):
    """(channel vals, connection vals, assistant vals) of 18.0.1.x-style vals."""
    channel, connection, assistant = {}, {}, {}
    for key, value in vals.items():
        if key in CONNECTION_FIELDS:
            connection[CONNECTION_FIELDS[key]] = value
        elif key in ASSISTANT_FIELDS:
            assistant[ASSISTANT_FIELDS[key]] = value
        else:
            channel[key] = value
    if 'timeout_seconds' in connection and not connection['timeout_seconds']:
        # 18.0.1.x: 0 meant the backend's default
        connection['timeout_seconds'] = N8N_DEFAULT_TIMEOUT if connection.get('backend_type') == 'n8n' \
            else LITELLM_DEFAULT_TIMEOUT
    return channel, connection, assistant


def make_connection(env, name='Test Connection', **vals):
    """An AI connection (as superuser). Without values: the test LiteLLM backend;
    with values: exactly those (as an 18.0.1.x channel had them)."""
    if vals:
        values = {'name': name, 'backend_type': 'openai'}
    else:
        values = {'name': name, 'backend_type': 'openai', 'endpoint_url': TEST_BASE_URL, 'api_key': TEST_API_KEY,
                  'model': TEST_MODEL}
    values.update(vals)
    return env['im_livechat.ai.connection'].sudo().create(values)


def make_assistant(env, connection=None, name='AI 客服助理', **vals):
    """An AI assistant available in Live Chat (as superuser)."""
    values = {'name': name, 'connection_id': (connection or make_connection(env)).id, 'livechat_available': True}
    values.update(vals)
    return env['im_livechat.ai.assistant'].sudo().create(values)


def make_ai_channel(env, vals=None, user=None):
    """A Live Chat channel from 18.0.1.x-style vals: the connection and assistant
    fields go to a new AI connection and AI assistant, the rest to the channel.
    Without any backend value the channel has no assistant. Created as `user`
    (default: superuser)."""
    channel_vals, connection_vals, assistant_vals = split_ai_vals(dict(vals or {}))
    if connection_vals or assistant_vals:
        connection = make_connection(env, name='%s connection' % channel_vals.get('name', 'Test'),
                                     **connection_vals)
        assistant = make_assistant(env, connection, **assistant_vals)
        channel_vals.setdefault('ai_assistant_id', assistant.id)
    Channel = env['im_livechat.channel']
    Channel = Channel.with_user(user) if user else Channel.sudo()
    return Channel.create(channel_vals)


def set_ai(channel, vals):
    """Write 18.0.1.x-style vals: connection and assistant values go to the
    channel's AI assistant and connection (created when missing), the rest to
    the channel (as superuser)."""
    env = channel.env
    channel_vals, connection_vals, assistant_vals = split_ai_vals(dict(vals))
    assistant = channel.sudo().ai_assistant_id
    if (connection_vals or assistant_vals) and not assistant:
        connection = make_connection(env, name='%s connection' % channel.name, **connection_vals)
        assistant = make_assistant(env, connection, **assistant_vals)
        channel_vals.setdefault('ai_assistant_id', assistant.id)
        connection_vals = assistant_vals = {}
    if connection_vals:
        assistant.connection_id.sudo().write(connection_vals)
    if assistant_vals:
        assistant.sudo().write(assistant_vals)
    if channel_vals:
        channel.sudo().write(channel_vals)
    return channel


def connection_of(channel):
    return channel.sudo().ai_assistant_id.connection_id


def assistant_of(channel):
    return channel.sudo().ai_assistant_id


def setup_backend(channel, base_url=TEST_BASE_URL, api_key=TEST_API_KEY, log_payload=None):
    """Give an existing channel the test connection (as superuser)."""
    vals = {'ai_backend_type': 'openai', 'ai_endpoint_url': base_url, 'ai_api_token': api_key,
            'ai_model': TEST_MODEL}
    if log_payload is not None:
        vals['ai_log_payload'] = log_payload
    set_ai(channel, vals)


def livechat_operator(env):
    """Partner used as livechat_operator_id (required on livechat sessions)."""
    return env.ref('base.partner_admin')


def run_ai_job(env, session, message=None):
    """Queue a reply job for `session` (latest message by default) and run it
    synchronously in the test transaction (no thread, no commit)."""
    if message is None:
        message = env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', session.id),
        ], order='id desc', limit=1)
    job = env['im_livechat.ai.job']._create_for_message(session, message)
    job._run_job(commit=False)
    return job


def make_session(channel, text='test', visitor=None):
    """A live chat session of `channel` with one visitor message (created
    directly: no job is queued)."""
    env = channel.env
    session = env['discuss.channel'].create({
        'name': 'Backend Call Session',
        'channel_type': 'livechat',
        'livechat_operator_id': livechat_operator(env).id,
        'livechat_channel_id': channel.id,
    })
    if visitor is None:
        visitor = env['res.partner'].create({'name': 'Backend Call Visitor'})
    if text:
        env['mail.message'].create({
            'body': text,
            'model': 'discuss.channel',
            'res_id': session.id,
            'message_type': 'comment',
            'author_id': visitor.id,
        })
    return session


def call_backend(channel, text='test', session=None, before_call=None):
    """One reply through the real request path (_generate_ai_reply)."""
    if session is None:
        session = make_session(channel, text)
    return channel._generate_ai_reply(session, session._ai_conversation(channel), before_call=before_call)


def ok_response(content='OK', usage=None):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': {'content': content}}],
        'usage': usage if usage is not None else {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
    }
    return response


# 18.0.1.x columns of im_livechat_channel (kept one version on upgraded databases; absent on a fresh 2.0.0
# install). The tests of the 18.0.1.2.x migration scripts, which still run on the way up from 18.0.1.1.x, and
# of the 18.0.2.0.0 migration add them in their transaction.
LEGACY_CHANNEL_COLUMNS = (
    ('ai_backend_type', 'VARCHAR'), ('ai_endpoint_url', 'VARCHAR'), ('ai_api_token_secret', 'VARCHAR'),
    ('ai_model', 'VARCHAR'), ('ai_document_mode', 'VARCHAR'), ('ai_system_prompt', 'TEXT'),
    ('ai_max_history', 'INTEGER'), ('ai_max_retries', 'INTEGER'), ('ai_request_timeout', 'INTEGER'),
    ('ai_error_message', 'TEXT'), ('ai_bot_partner_id', 'INTEGER'), ('ai_bot_name', 'VARCHAR'),
    ('ai_log_payload', 'BOOLEAN'), ('ai_log_retention_days', 'INTEGER'),
)


def add_legacy_channel_columns(cr):
    for column, kind in LEGACY_CHANNEL_COLUMNS:
        cr.execute('ALTER TABLE im_livechat_channel ADD COLUMN IF NOT EXISTS %s %s' % (column, kind))


def legacy_channel_values(cr, channel_id, *columns):
    """{column: value} of the 18.0.1.x columns of one channel (SQL)."""
    cr.execute('SELECT %s FROM im_livechat_channel WHERE id = %%s' % ', '.join(columns), (channel_id,))
    return dict(zip(columns, cr.fetchone()))


def set_legacy_channel_values(cr, channel_id, **values):
    """Write 18.0.1.x columns of one channel (SQL)."""
    assignments = ', '.join('%s = %%s' % column for column in values)
    cr.execute('UPDATE im_livechat_channel SET %s WHERE id = %%s' % assignments, (*values.values(), channel_id))
