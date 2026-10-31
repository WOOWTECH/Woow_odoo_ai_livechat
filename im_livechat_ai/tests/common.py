# -*- coding: utf-8 -*-
"""Shared fixtures for im_livechat_ai tests (no real HTTP, no real key)."""

import json

from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    DEFAULT_MODEL,
    ICP_API_KEY,
    ICP_BASE_URL,
    ICP_LOG_PAYLOAD,
    ICP_MODEL_CACHE,
)

TEST_BASE_URL = 'https://litellm.test/v1'
TEST_API_KEY = 'sk-test-key'
# Models the tests choose; channels may only choose cached models (P1-2).
TEST_MODELS = [DEFAULT_MODEL, 'gpt-4o', 'other-pool/other-model']


def setup_litellm(env, base_url=TEST_BASE_URL, api_key=TEST_API_KEY, log_payload=False,
                  models=TEST_MODELS):
    """Configure the system-level LiteLLM settings used by every channel
    (and the cached model list of "Fetch Available Models")."""
    ICP = env['ir.config_parameter'].sudo()
    ICP.set_param(ICP_BASE_URL, base_url)
    ICP.set_param(ICP_API_KEY, api_key)
    ICP.set_param(ICP_LOG_PAYLOAD, log_payload)
    if models is not None:
        ICP.set_param(ICP_MODEL_CACHE, json.dumps(list(models)))


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
