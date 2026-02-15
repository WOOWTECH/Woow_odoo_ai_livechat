# AI Chatbot for Livechat / AI 線上客服機器人

[English](#english) | [中文](#中文)

---

## English

### Overview

Enhance Odoo Livechat with AI chatbots leveraging predefined Data Sources. This module integrates AI assistants into Odoo's livechat system, providing automated customer support with intelligent responses.

### Requirements

- Odoo 18.0
- PostgreSQL 13+ with pgvector extension
- Minimum 8GB RAM (16GB recommended)
- `im_livechat` module enabled
- `ai_mail_gt` module installed

### Installation

1. Place this module in your Odoo addons directory
2. Update the apps list: Settings > Apps > Update Apps List
3. Search for "AI Chatbot for Livechat" and install

### Configuration

1. Navigate to **Livechat > Configuration > Channels**
2. Select or create a livechat channel
3. In the "Operators" tab, find the "AI Operators" section
4. Select an AI Assistant to handle conversations
5. Optionally customize the AI Context for channel-specific behavior

### Features

- **AI-Powered Responses**: 24/7 automated customer support
- **Human Handover**: Seamless escalation to human operators
- **Custom AI Context**: Configure AI behavior per channel
- **Multi-AI Support**: Use different AI models (ChatGPT, Claude, Gemini)
- **LINE Integration**: AI responses automatically forwarded to LINE users (requires `woow_odoo_livechat_line` module)

### AI Model Connectors

Optional connector modules for different AI providers:
- `ai_chatgpt_gt` - OpenAI ChatGPT
- `ai_claude_gt` - Anthropic Claude
- `ai_gemini_gt` - Google Gemini

### LINE Integration

When used with `woow_odoo_livechat_line` module, AI responses are automatically sent to LINE users in real-time. The integration ensures:
- Immediate forwarding of AI responses to LINE
- Seamless conversation flow between LINE and Odoo
- No message delays or synchronization issues

### Support

- Email: gt.apps.odoo@gmail.com
- Documentation: https://ai-docs-18.gt-apps.top
- Live Demo: https://ai-demo-18.gt-apps.top

### License

OPL-1 (Odoo Proprietary License)

---

## 中文

### 概述

透過 AI 聊天機器人增強 Odoo 線上客服功能，利用預定義的資料來源提供智能回應。此模組將 AI 助手整合到 Odoo 的線上客服系統中，提供自動化的客戶支援服務。

### 系統需求

- Odoo 18.0
- PostgreSQL 13+ 並安裝 pgvector 擴充套件
- 最低 8GB 記憶體（建議 16GB）
- 啟用 `im_livechat` 模組
- 安裝 `ai_mail_gt` 模組

### 安裝步驟

1. 將此模組放置於您的 Odoo addons 目錄
2. 更新應用程式列表：設定 > 應用程式 > 更新應用程式列表
3. 搜尋「AI Chatbot for Livechat」並安裝

### 設定方式

1. 前往 **線上客服 > 設定 > 頻道**
2. 選擇或建立一個線上客服頻道
3. 在「客服人員」分頁中，找到「AI 客服人員」區塊
4. 選擇一個 AI 助手來處理對話
5. 可選擇性地自訂 AI 情境以調整頻道特定行為

### 功能特色

- **AI 智能回應**：全天候 24/7 自動化客戶支援
- **人工接手**：無縫轉接至真人客服
- **自訂 AI 情境**：可依頻道配置 AI 行為
- **多 AI 模型支援**：使用不同的 AI 模型（ChatGPT、Claude、Gemini）
- **LINE 整合**：AI 回應自動轉發至 LINE 用戶（需安裝 `woow_odoo_livechat_line` 模組）

### AI 模型連接器

可選的 AI 供應商連接模組：
- `ai_chatgpt_gt` - OpenAI ChatGPT
- `ai_claude_gt` - Anthropic Claude
- `ai_gemini_gt` - Google Gemini

### LINE 整合功能

當與 `woow_odoo_livechat_line` 模組搭配使用時，AI 回應會即時自動發送至 LINE 用戶。此整合確保：
- AI 回應即時轉發至 LINE
- LINE 與 Odoo 之間的對話無縫流轉
- 無訊息延遲或同步問題

### 技術支援

- 電子郵件：gt.apps.odoo@gmail.com
- 文件說明：https://ai-docs-18.gt-apps.top
- 線上示範：https://ai-demo-18.gt-apps.top

### 授權條款

OPL-1（Odoo 專有授權）
