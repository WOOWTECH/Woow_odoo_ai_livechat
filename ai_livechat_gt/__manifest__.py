{
    'name': "AI Chatbot for Livechat",
    'summary': """
Enhance Odoo Livechat with AI chatbots leveraging predefined Data Sources.
Can use other AI models like ChatGPT, Gemini, Claude, etc through connector modules.
""",
    'sequence': 150,
    'description': """
Enhance Odoo Livechat with AI chatbots leveraging predefined Data Sources.
Can use other AI models like ChatGPT, Gemini, Claude, etc through connector modules.
""",
    'author': "GT Apps",
    'support': 'gt.apps.odoo@gmail.com',
    'live_test_url': 'https://ai-demo.gt-apps.top',
    'category': 'Productivity/AI',
    'version': '0.2.0',
    'depends': ['im_livechat', 'ai_mail_gt'],
    'data': [
        'security/ir.model.access.csv',
        'data/data.xml',
        'views/im_livechat_channel_views.xml',
    ],
    'demo': [],
    'assets': {
        # Note: Assets from ai_mail_gt are inherited through module dependency.
        # This explicit reference ensures livechat embed includes AI UI components.
        # If ai_mail_gt structure changes, this path may need updating.
        'im_livechat.assets_embed_core': [
            'ai_mail_gt/static/src/core/common/**/*',
        ],
    },
    'images': ['static/description/banner.jpg'],
    'installable': True,
    'application': False,
    'auto_install': True,
    'license': 'OPL-1',
    'price': 29.9,
    'currency': 'USD',
}
