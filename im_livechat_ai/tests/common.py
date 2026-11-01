# -*- coding: utf-8 -*-
"""Shared fixtures for im_livechat_ai tests (no real HTTP, no real key)."""

from unittest.mock import MagicMock

TEST_BASE_URL = 'https://litellm.test/v1'
TEST_API_KEY = 'dummy-test-token'
TEST_MODEL = 'chatgpt-pool/gpt-6-sol'
# Backend connection of an AI-enabled test channel (OpenAI compatible).
# Spread first in a create() dict, so explicit keys of the test win.
TEST_BACKEND = {
    'ai_backend_type': 'openai',
    'ai_endpoint_url': TEST_BASE_URL,
    'ai_api_token': TEST_API_KEY,
    'ai_model': TEST_MODEL,
}
TEST_N8N_URL = 'https://n8n.test/webhook/livechat-ai'
TEST_N8N_SECRET = 'dummy-signing-secret'
TEST_N8N_BACKEND = {
    'ai_backend_type': 'n8n',
    'ai_endpoint_url': TEST_N8N_URL,
    'ai_api_token': TEST_N8N_SECRET,
}


def setup_backend(channel, base_url=TEST_BASE_URL, api_key=TEST_API_KEY, log_payload=None):
    """Give an existing channel the test connection (as superuser)."""
    vals = {'ai_backend_type': 'openai', 'ai_endpoint_url': base_url, 'ai_api_token': api_key}
    if log_payload is not None:
        vals['ai_log_payload'] = log_payload
    channel.sudo().write(vals)


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
