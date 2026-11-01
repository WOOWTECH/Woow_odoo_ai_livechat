# -*- coding: utf-8 -*-
{
    'name': 'Live Chat AI Integration',
    'version': '18.0.1.2.0',
    'category': 'Website/Live Chat',
    'summary': 'Answer Live Chat visitors through a per-channel AI backend (OpenAI compatible or n8n)',
    'description': """
Live Chat AI Integration
=========================

Answers Live Chat visitors through an AI backend chosen per channel. Odoo
records, forwards and manages the conversation; the backend does everything
else (persona, tools such as product lookup, moderation, permissions).

Features:
---------
* Per-channel backend connection (Live Chat > Configuration > Channels >
  AI tab), set by system administrators: OpenAI compatible
  (/v1/chat/completions, e.g. LiteLLM or Hermes, Bearer token) or an n8n
  webhook (HMAC-SHA256 signed requests, the secret is never sent)
* The access token is write-only: never shown again, never readable by Live
  Chat managers; endpoint URLs must use https (http only for localhost and
  hosts ending in .svc.cluster.local)
* Conversation history context for multi-turn dialogue, the channel's
  optional system prompt, a stable session id and the visitor identity
* Per-channel bot partner with custom name
* Durable reply jobs: queued in the visitor's transaction, processed after
  commit, one at a time per conversation (rapid messages get one reply),
  recovered by a cron after a restart
* Bounded backend calls (45 s timeout, at most 2 retries; n8n only retried
  when the call could not be delivered or got HTTP 429/503); on failure the
  visitor gets an error message and the operators are notified
* Portal (logged-in) customers are answered like guests; internal users,
  the session operator and the bots never trigger the AI
* Human handoff: an operator's reply pauses the AI for the conversation
  (30 minutes by default, per channel, extended by every operator reply);
  explicit visitor requests such as "找真人" or "talk to a human" send a
  handoff notice once, notify the operators and wait for an operator
  (10 minutes by default); the first visitor message during the wait gets
  one waiting notice, and if no operator replies the AI answers what the
  visitor wrote meanwhile when the wait ends (recovery cron)
* AI disclosure: robot avatar on the AI bot and an AI welcome message in the
  chat window
* The AI tab shows the last run of the reply recovery cron and stuck jobs
* Operator replies are sent to the backend as colleague replies
  ("[真人客服 name]") so the AI does not contradict them
* AI API logs with token usage and HTTP status (Live Chat administrators
  only; conversation content is not stored unless enabled per channel;
  retention per channel)

Requirements:
-------------
* Odoo 18.0+
* im_livechat module
* An OpenAI compatible server (LiteLLM, Hermes, ...) or an n8n instance

Configuration:
--------------
1. A system administrator opens Live Chat > Configuration > Channels >
   (channel) > AI tab, picks the backend type, fills in the endpoint URL,
   the access token (and the model for LiteLLM) and clicks
   "Test Connection".
2. A Live Chat manager enables AI on that channel.
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

        # Data
        'data/ai_data.xml',
    ],
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'auto_install': False,
    'application': False,
}
