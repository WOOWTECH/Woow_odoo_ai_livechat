# -*- coding: utf-8 -*-
{
    'name': 'Live Chat N8N Integration',
    'version': '18.0.1.0.0',
    'category': 'Website/Live Chat',
    'summary': 'Integrate Odoo livechat with n8n workflow automation',
    'description': """
Live Chat N8N Integration
==========================

This module integrates Odoo Live Chat with n8n workflow automation platform.

Features:
---------
* Send live chat messages to n8n webhooks
* Trigger n8n workflows based on chat events
* Flexible webhook configuration per live chat channel
* Support for custom message formatting

Requirements:
-------------
* Odoo 18.0+
* im_livechat module
* n8n instance with webhook configuration

Configuration:
--------------
After installation, configure webhook URLs in:
Settings > Live Chat > Channels > N8N Integration tab
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/im_livechat_n8n',
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
        'views/n8n_webhook_log_views.xml',

        # Data
        'data/n8n_data.xml',
    ],
    'demo': [
        # 'demo/demo_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
