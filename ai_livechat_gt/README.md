# AI Chatbot for Livechat

Enhance Odoo Livechat with AI chatbots leveraging predefined Data Sources.

## Requirements

- Odoo 18.0
- PostgreSQL 13+ with pgvector extension
- Minimum 8GB RAM (16GB recommended)
- `im_livechat` module enabled
- `ai_mail_gt` module installed

## Installation

1. Place this module in your Odoo addons directory
2. Update the apps list: Settings > Apps > Update Apps List
3. Search for "AI Chatbot for Livechat" and install

## Configuration

1. Navigate to **Livechat > Configuration > Channels**
2. Select or create a livechat channel
3. In the "Operators" tab, find the "AI Operators" section
4. Select an AI Assistant to handle conversations
5. Optionally customize the AI Context for channel-specific behavior

## Features

- **AI-Powered Responses**: 24/7 automated customer support
- **Human Handover**: Seamless escalation to human operators
- **Custom AI Context**: Configure AI behavior per channel
- **Multi-AI Support**: Use different AI models (ChatGPT, Claude, Gemini)

## AI Model Connectors

Optional connector modules for different AI providers:
- `ai_chatgpt_gt` - OpenAI ChatGPT
- `ai_claude_gt` - Anthropic Claude
- `ai_gemini_gt` - Google Gemini

## Support

- Email: gt.apps.odoo@gmail.com
- Documentation: https://ai-docs-18.gt-apps.top
- Live Demo: https://ai-demo-18.gt-apps.top

## License

OPL-1 (Odoo Proprietary License)
