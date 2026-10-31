# -*- coding: utf-8 -*-
{
    'name': 'Live Chat AI Integration',
    'version': '18.0.1.1.0',
    'category': 'Website/Live Chat',
    'summary': 'Integrate Odoo livechat with OpenAI-compatible LLM APIs',
    'description': """
Live Chat AI Integration
=========================

This module integrates Odoo Live Chat with OpenAI-compatible LLM APIs
for automated AI-powered customer support.

Features:
---------
* Calls the company LiteLLM gateway (OpenAI-compatible /v1/chat/completions)
* System-wide LiteLLM URL and API key, managed by administrators only
* Per-channel AI configuration (model from the LiteLLM model list, system prompt)
* Conversation history context for multi-turn dialogue
* Per-channel bot partner with custom name
* Durable reply jobs: queued in the visitor's transaction, processed after
  commit, one at a time per conversation (rapid messages get one reply),
  recovered by a cron after a restart
* Bounded LLM calls (45 s timeout, at most 2 retries); on failure the visitor
  gets an error message and the operators are notified
* API call logging with token usage tracking (Live Chat administrators only;
  conversation content is not stored unless explicitly enabled)

Requirements:
-------------
* Odoo 18.0+
* im_livechat module
* A LiteLLM (or other OpenAI-compatible) gateway and API key

Configuration:
--------------
1. A system administrator sets the LiteLLM URL and API key in
   Settings > Live Chat AI and clicks "Fetch Available Models".
2. Live Chat managers enable AI and choose a model per channel in
   Live Chat > Configuration > Channels > AI Integration tab.
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_ai_livechat',
    'license': 'LGPL-3',
    'external_dependencies': {'python': ['requests']},
    'depends': [
        'im_livechat',
        'mail',
    ],
    'data': [
        # Security
        'security/ir.model.access.csv',

        # Views
        'views/im_livechat_channel_views.xml',
        'views/llm_api_log_views.xml',
        'views/livechat_ai_job_views.xml',
        'views/res_config_settings_views.xml',

        # Data
        'data/ai_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
