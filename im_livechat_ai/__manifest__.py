# -*- coding: utf-8 -*-
{
    'name': 'Live Chat AI Integration',
    'version': '18.0.2.1.0',
    'category': 'Website/Live Chat',
    'summary': 'Answer Live Chat visitors with an AI assistant (LiteLLM or n8n), configured in two layers: '
               'AI connections and AI assistants',
    'description': """
Live Chat AI Integration
=========================

Answers Live Chat visitors with an AI assistant. Odoo records, forwards and
manages the conversation; the backend does everything else (persona, tools
such as product lookup, moderation, permissions). Backends: LiteLLM (OpenAI
compatible /v1/chat/completions, Bearer key) or an n8n webhook (HMAC-SHA256
signed requests, the secret is never sent). Nothing else.

Two layers, aligned with Discuss AI and Project AI:
---------------------------------------------------

Each product has its own core; nothing is shared.


* AI Connections (「AI 連線」, system administrators only): backend type,
  endpoint URL, write-only API key, model, timeout of one answer, retries,
  documents, content logging, "Safe for Customers", readiness and Test
  Connection
* AI Assistants (「AI 助理」, system administrators only): the name visitors
  see, avatar and archived bot partner, system prompt, error message,
  history length, the AI connection, and "Available in Live Chat"
  (「可用在 Live Chat」)
* Live Chat channel, tab "AI Assistant" (Live Chat managers): pick one of the
  assistants available in Live Chat, turn the AI on; welcome and handoff
  texts, pause and wait minutes, keywords; the status of the AI

Features:
---------
* The API key is write-only: never shown again, removed when the endpoint
  host or the backend type changes; endpoint URLs must use https (http only
  for localhost and hosts ending in .svc.cluster.local)
* Conversation history, the assistant's optional system prompt, a stable
  session id and the visitor identity (never the email for LiteLLM)
* Visitor images and files go to the backend as they are (no text
  extraction): LiteLLM gets images as data URIs and documents of at most
  4 MB as file parts (or signed links); n8n gets signed download URLs
  (/im_livechat_ai/attachment/..., valid 1 hour, live chat attachments
  only). Dangerous types are links only
* Durable reply jobs: queued in the visitor's transaction, processed after
  commit, one at a time per conversation (rapid messages get one reply),
  recovered by a cron after a restart; at most 30 unfinished replies per
  channel (system parameter im_livechat_ai.max_active_jobs)
* The connection's timeout bounds one answer, retries included (LiteLLM 45 s,
  n8n 120 s by default, 10-300 s); retries only when the request was not
  sent or the backend answered HTTP 429/503; on failure the visitor gets the
  error message and the operators are notified
* When the AI is on but cannot answer, no job is queued: the conversation
  gets one notice and the operators one alert per blocked period
* Human handoff: an operator's reply pauses the AI for the conversation;
  the backend decides when the visitor wants a human ([[handoff]] or n8n
  "handoff": true); Odoo waits for an operator, then the AI answers again
* Every request carries the current time (company time zone, else the
  visitor's, else UTC); a speaker label a model copies to the start of its
  answer is removed; a file the model cannot read is sent again as a link
  and the conversation remembers it (contract v1.3)
* AI disclosure: robot avatar on the AI bot and an AI welcome message
* Call logs (one per request, kept 30 days) and monthly usage, system
  administrators only; content is stored only while enabled on the
  connection
* Rate limits live in the LiteLLM key; Odoo keeps only technical queue caps

Requirements:
-------------
* Odoo 18.0+
* im_livechat module
* A LiteLLM server or an n8n instance

Configuration:
--------------
1. A system administrator creates an AI connection (Live Chat >
   Configuration > AI Connections) and clicks "Test Connection".
2. A system administrator creates an AI assistant with that connection and
   ticks "Available in Live Chat" (Live Chat > Configuration > AI
   Assistants).
3. A Live Chat manager picks the assistant on the channel's "AI Assistant"
   tab and turns the AI on.
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
        'security/ir_rule.xml',

        # Views
        'views/ai_connection_views.xml',
        'views/ai_assistant_views.xml',
        'views/im_livechat_channel_views.xml',
        'views/llm_api_log_views.xml',
        'views/livechat_ai_job_views.xml',
        'views/ai_usage_views.xml',

        # Data
        'data/ai_data.xml',
    ],
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'auto_install': False,
    'application': False,
}
