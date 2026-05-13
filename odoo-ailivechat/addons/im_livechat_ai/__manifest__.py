# -*- coding: utf-8 -*-
{
    'name': 'Live Chat AI Integration',
    'version': '18.0.1.0.0',
    'category': 'Website/Live Chat',
    'summary': 'Integrate Odoo livechat with OpenAI-compatible LLM APIs',
    'description': """
Live Chat AI Integration
=========================

This module integrates Odoo Live Chat with OpenAI-compatible LLM APIs
for automated AI-powered customer support.

Features:
---------
* Direct LLM API calls (OpenAI, MiniMax, DeepSeek, Ollama, etc.)
* Per-channel AI configuration (API key, model, system prompt)
* Conversation history context for multi-turn dialogue
* Per-channel bot partner with custom name
* Configurable retry logic with error fallback messages
* API call logging with token usage tracking

Requirements:
-------------
* Odoo 18.0+
* im_livechat module
* An OpenAI-compatible API endpoint

Configuration:
--------------
After installation, configure AI settings in:
Settings > Live Chat > Channels > AI Integration tab
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_ai_livechat',
    'license': 'LGPL-3',
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

        # Data
        'data/ai_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
