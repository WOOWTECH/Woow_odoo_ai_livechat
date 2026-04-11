<div align="center">

# Odoo 即時聊天 N8N 整合模組

### `im_livechat_n8n`

將 Odoo 18 即時聊天與 n8n 工作流程自動化平台整合，實現 AI 驅動的客服聊天機器人

[![Odoo 18.0](https://img.shields.io/badge/Odoo-18.0-714B67?style=for-the-badge&logo=odoo&logoColor=white)](https://www.odoo.com)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![License: LGPL-3](https://img.shields.io/badge/License-LGPL--3-blue?style=for-the-badge)](https://www.gnu.org/licenses/lgpl-3.0)
[![n8n 2.x](https://img.shields.io/badge/n8n-2.x-EA4B71?style=for-the-badge&logo=n8n&logoColor=white)](https://n8n.io)
[![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org)

[English](README.md) | **繁體中文**

---

[概述](#概述) | [功能特色](#功能特色) | [功能截圖](#功能截圖) | [安裝說明](#安裝說明) | [設定指南](#設定指南) | [n8n 工作流程設定](#n8n-工作流程設定) | [安全機制](#安全機制) | [測試驗證](#測試驗證) | [API 參考](#api-參考) | [授權條款](#授權條款)

</div>

---

## 概述

**Live Chat N8N Integration** (`im_livechat_n8n`) 是一個 Odoo 18 自訂模組，將 Odoo 即時聊天系統與 [n8n](https://n8n.io) 工作流程自動化平台無縫整合。當網站訪客透過即時聊天小工具發送訊息時，模組會透過 webhook 將訊息傳送至 n8n。n8n 透過 LLM（如 OpenRouter/Gemini、OpenAI、Claude）處理後，將 AI 回覆傳回 Odoo，以「AI 助手」身分發布在即時聊天會話中。

本模組作為**無頭 RAG（Headless RAG）**系統運作 -- Odoo 負責前端 UI，n8n 負責智慧處理。

### 架構流程圖

```
網站訪客 → Odoo 即時聊天小工具 → discuss.channel.message_post()
    → im_livechat_n8n 觸發出站 webhook（守護線程）
    → n8n Webhook Trigger 接收訊息
    → n8n Code Node 提取 session_uuid、message、channel_id
    → n8n HTTP Request 呼叫 LLM API（OpenRouter/Gemini）
    → n8n Code Node 建構 Odoo 回覆內容
    → n8n HTTP Request POST 到 Odoo /im_livechat_n8n/webhook/reply
    → Odoo controller 驗證 API 金鑰、找到會話、發布機器人回覆
    → 訪客在小工具中看到 AI 助手回覆
```

### 模組目錄結構

```
im_livechat_n8n/
├── __init__.py
├── __manifest__.py
├── controllers/
│   ├── __init__.py
│   └── webhook.py                     # 入站 webhook 處理器 - 驗證 API 金鑰、UUID、訊息大小
├── models/
│   ├── __init__.py
│   ├── discuss_channel.py             # 出站觸發器 + webhook 迴圈防護
│   ├── im_livechat_channel.py         # 負載建構器 + 非同步分發含重試機制
│   └── n8n_webhook_log.py             # Webhook 活動記錄模型
├── views/
│   ├── im_livechat_channel_views.xml  # 即時聊天頻道表單的 N8N 整合分頁
│   └── n8n_webhook_log_views.xml      # Webhook 記錄樹狀/表單視圖
├── data/
│   └── n8n_data.xml                   # N8N Bot 合作夥伴 + 清理排程工作
├── security/
│   └── ir.model.access.csv
└── tests/
    └── test_im_livechat_n8n.py        # 單元測試
```

---

## 功能特色

### 核心技術特色

| 特色 | 說明 |
|------|------|
| **Webhook 迴圈防護** | `_is_visitor_message()` 方法排除 N8N Bot、OdooBot 及任何具有 `user_ids` 的合作夥伴，確保機器人回覆不會觸發二次 webhook |
| **非同步分發** | 守護線程含 3 次重試 + 指數退避機制（1s、2s、4s），絕不阻塞聊天操作 |
| **執行緒安全** | ORM 欄位值在產生線程前擷取，避免跨線程存取 ORM 記錄的問題 |
| **API 金鑰安全** | 使用 `secrets.token_urlsafe(32)` 產生加密安全的 API 金鑰，支援金鑰輪換 |
| **訊息驗證** | UUID 模式驗證（`^[A-Za-z0-9_-]{6,50}$`）、10KB 訊息大小限制、UTF-8 編碼檢查 |
| **Webhook 記錄** | 完整的雙向記錄（出站/入站）含成功/失敗/逾時狀態及回應時間 |
| **清理排程** | 排程動作自動清除超過 30 天的 webhook 記錄 |

### 功能列表

- **出站 Webhook**: 訪客發送訊息時自動通知 n8n
- **入站 Webhook**: 接收 n8n 工作流程的自動回覆並張貼至聊天會話
- **每頻道獨立設定**: 為每個即時聊天頻道獨立配置 webhook 參數
- **Webhook 記錄管理**: 追蹤所有 webhook 活動，方便除錯與監控
- **錯誤處理**: 健全的重試邏輯搭配指數退避（3 次重試）
- **安全性**: API 金鑰認證、輸入驗證、存取控制
- **測試 Webhook 連線**: 一鍵測試 webhook 連線功能

---

## 功能截圖

### 即時聊天頻道概覽

即時聊天頻道列表頁面，顯示所有已設定的聊天頻道。

![即時聊天頻道概覽](docs/screenshots/livechat_channel_list.png)

### N8N 整合設定分頁

在即時聊天頻道表單中的「N8N 整合」分頁，可設定 webhook URL、API 金鑰及啟用/停用整合。

![N8N 整合設定分頁](docs/screenshots/livechat_n8n_tab.png)

### Webhook 活動記錄

完整的 webhook 活動記錄列表，顯示方向（出站/入站）、狀態、回應時間及 HTTP 狀態碼。

![Webhook 活動記錄](docs/screenshots/webhook_log_list.png)

### 管理員 Discuss 視圖

管理員在 Discuss 視圖中查看 AI 助手與訪客的對話內容。

![管理員 Discuss 視圖](docs/screenshots/admin_discuss_view.png)

### 訪客端即時聊天

訪客在網站上透過即時聊天小工具與 AI 助手互動的畫面。

![訪客端即時聊天](docs/screenshots/visitor_livechat_page.png)

### 測試結果截圖

| 測試 | 截圖 |
|------|------|
| 基礎英文問候測試 | ![測試 Round 01](docs/screenshots/test_round01_hello.png) |
| 中文 UTF-8 測試 | ![測試 Round 02](docs/screenshots/test_round02_chinese.png) |
| 組合壓力測試（表情符號+CJK+HTML+URL） | ![測試 Round 20](docs/screenshots/test_round20_stress.png) |

---

## 安裝說明

### 前置需求

| 項目 | 版本需求 |
|------|----------|
| Odoo | 18.0+ |
| Python | 3.10+ |
| PostgreSQL | 16+ |
| n8n | 2.x（自架或雲端皆可） |
| Odoo 相依模組 | `im_livechat`、`mail` |

### 方法一：模組安裝

1. **將模組複製到 Odoo addons 目錄**：

   ```bash
   cd /path/to/odoo/addons
   git clone https://github.com/WOOWTECH/im_livechat_n8n.git
   ```

2. **更新應用程式列表**：
   - 前往 **應用程式** 選單
   - 點選「更新應用程式列表」

3. **安裝模組**：
   - 搜尋「Live Chat N8N」
   - 點選「安裝」

### 方法二：Docker 部署

本專案提供完整的 Docker Compose 設定，包含 Odoo 18 + PostgreSQL 16。

1. **啟動服務**：

   ```bash
   cd odoo-n8nlivechat
   docker-compose up -d
   ```

2. **服務連接埠**：

   | 服務 | 連接埠 | 說明 |
   |------|--------|------|
   | Odoo | `9094` | Odoo 18 網頁介面（對應容器內 8069） |
   | n8n | `15678` | n8n 工作流程編輯器 |
   | PostgreSQL | 內部 | PostgreSQL 16 資料庫（僅內部網路存取） |

3. **網路架構**：
   - Odoo 與 n8n 透過共用 Docker 網路 `odoo-n8nlivechat-network` 進行內部通訊
   - 容器間可透過服務名稱互相存取

4. **初次設定**：
   - 開啟瀏覽器存取 `http://localhost:9094`
   - 完成 Odoo 初始化設定
   - 安裝 `im_livechat_n8n` 模組

---

## 設定指南

### 步驟 1：設定 Odoo 即時聊天頻道

1. 前往 **網站 > 即時聊天 > 頻道**
2. 選擇現有頻道或建立新頻道
3. 切換至「**N8N Integration**」分頁
4. 勾選「**Enable N8N Integration**」
5. 填入 n8n webhook URL（例如：`http://n8n:5678/webhook/livechat-handler`）
6. 複製自動產生的 **API Key**（稍後設定 n8n 時使用）

### 步驟 2：設定 n8n 工作流程

請參閱下方「[n8n 工作流程設定](#n8n-工作流程設定)」章節。

### 步驟 3：測試連線

1. 回到 Odoo 即時聊天頻道的 N8N 分頁
2. 點選「**Test Webhook Connection**」按鈕
3. 確認 n8n 工作流程接收到測試訊息

### Webhook URL 格式

```
https://your-n8n-instance.com/webhook/livechat-handler
```

> **注意**：Docker 環境下，Odoo 容器可透過 `http://n8n:5678/webhook/...` 存取 n8n 服務。

### API 金鑰管理

- API 金鑰在頻道建立時自動產生
- 點選「**Regenerate API Key**」按鈕可重新產生金鑰
- 在 n8n HTTP Request 節點的 headers 中設定 `X-API-Key` 為此金鑰值

---

## n8n 工作流程設定

本模組使用 5 節點管線架構：

### 節點 1：Webhook Trigger（接收 Odoo 訊息）

- **節點類型**：Webhook
- **HTTP 方法**：POST
- **路徑**：`/livechat-handler`
- **說明**：接收 Odoo 發送的即時聊天訊息

### 節點 2：Extract Message（Code 節點）

提取訊息關鍵資訊：

```javascript
const sessionUuid = $json.session.uuid;
const messageBody = $json.message.body;
const channelId = $json.channel.id;
const visitorName = $json.session.visitor_name || 'Visitor';
const callbackUrl = $json.metadata.callback_url;

return {
  json: {
    session_uuid: sessionUuid,
    message: messageBody,
    channel_id: channelId,
    visitor_name: visitorName,
    callback_url: callbackUrl
  }
};
```

### 節點 3：Call LLM（HTTP Request 呼叫 OpenRouter API）

- **HTTP 方法**：POST
- **URL**：`https://openrouter.ai/api/v1/chat/completions`（或其他 LLM API）
- **Headers**：
  - `Authorization`: `Bearer YOUR_OPENROUTER_API_KEY`
  - `Content-Type`: `application/json`
- **Request Body**：
  ```json
  {
    "model": "google/gemini-2.0-flash-001",
    "messages": [
      {
        "role": "system",
        "content": "You are a helpful customer support assistant."
      },
      {
        "role": "user",
        "content": "{{ $json.message }}"
      }
    ]
  }
  ```

> **提示**：可替換為 OpenAI、Claude、Gemini 或其他相容 API。

### 節點 4：Build Reply（Code 節點）

建構回傳 Odoo 的回覆內容：

```javascript
const aiResponse = $json.choices[0].message.content;
const sessionUuid = $('Extract Message').first().json.session_uuid;
const callbackUrl = $('Extract Message').first().json.callback_url;

return {
  json: {
    callback_url: callbackUrl,
    payload: {
      action: "send_message",
      session_uuid: sessionUuid,
      message: {
        body: aiResponse,
        author_name: "AI Assistant"
      }
    }
  }
};
```

### 節點 5：Send to Odoo（HTTP Request 回傳 Odoo webhook）

- **HTTP 方法**：POST
- **URL**：`{{ $json.callback_url }}`（或 `http://odoo:8069/im_livechat_n8n/webhook`）
- **Headers**：
  - `Content-Type`: `application/json`
  - `X-API-Key`: `YOUR_ODOO_API_KEY`（從 Odoo 頻道設定複製）
- **Request Body**：`{{ $json.payload }}`

### 完整流程圖

```
[Webhook Trigger] → [Extract Message] → [Call LLM] → [Build Reply] → [Send to Odoo]
```

---

## 安全機制

### API 金鑰認證

- 所有入站 webhook 請求必須在 `X-API-Key` header 中攜帶有效的 API 金鑰
- API 金鑰使用 `secrets.token_urlsafe(32)` 產生，具備加密安全性
- 每個即時聊天頻道擁有獨立的 API 金鑰
- 支援金鑰輪換功能

### 輸入驗證

| 驗證項目 | 規則 |
|----------|------|
| Session UUID | 必須符合 `^[A-Za-z0-9_-]{6,50}$` 模式 |
| 訊息大小 | 不得超過 10KB（UTF-8 編碼） |
| JSON 格式 | 嚴格的 JSON 解析及必填欄位檢查 |
| Webhook URL | 必須以 `http://` 或 `https://` 開頭 |

### Webhook 迴圈防護

`_is_visitor_message()` 方法確保只有訪客訊息觸發出站 webhook：

1. **排除 N8N Bot**：模組自定義的機器人合作夥伴（`im_livechat_n8n.partner_n8n_bot`）
2. **排除 OdooBot**：系統內建的 OdooBot（`base.partner_root`）
3. **排除內部使用者**：任何具有 `user_ids` 的合作夥伴（即 Odoo 內部帳號）

### 安全最佳實踐

1. **使用 HTTPS**：正式環境務必使用 HTTPS URL
2. **定期輪換金鑰**：定期或在金鑰洩露時重新產生 API 金鑰
3. **網路安全**：盡可能限制 webhook 端點僅接受已知 IP 位址的請求
4. **訊息驗證**：模組自動驗證訊息大小（10KB 限制）
5. **監控記錄**：定期檢視 webhook 記錄以偵測異常活動

---

## 測試驗證

### 測試概覽

模組通過了 **20 輪全面性測試**，涵蓋三大類別，全數通過。

| 類別 | 測試輪數 | 涵蓋範圍 |
|------|----------|----------|
| **完整性測試** | 7 輪 | 基本通訊、多語言、特殊字元、長訊息 |
| **穩定性測試** | 6 輪 | 快速連續發送、並行會話、會話恢復 |
| **邊緣案例測試** | 7 輪 | HTML 注入、表情符號、CJK 字元、URL、組合壓力測試 |

### 測試結果摘要

| 指標 | 數值 |
|------|------|
| 測試輪數 | **20/20 通過** |
| 出站 Webhook | 47 次 |
| 入站 Webhook | 45 次 |
| 失敗次數 | **0** |

### 代表性測試輪次

| 輪次 | 名稱 | 類別 | 測試內容 |
|------|------|------|----------|
| Round 01 | Hello | 完整性 | 基礎英文問候訊息 |
| Round 02 | Chinese | 完整性 | 中文 UTF-8 編碼訊息 |
| Round 04 | Admin | 完整性 | 管理員視角驗證 |
| Round 10 | Rapid Fire | 穩定性 | 快速連續發送多條訊息 |
| Round 15 | Emoji Flood | 邊緣案例 | 大量表情符號處理 |
| Round 18 | HTML Injection | 邊緣案例 | HTML 標籤注入防護 |
| Round 20 | Stress Combo | 邊緣案例 | 表情符號+CJK+HTML+URL 組合壓力測試 |

---

## API 參考

### 出站 Webhook（Odoo → n8n）

當訪客發送訊息時，以下 JSON 負載將傳送至 n8n webhook：

```json
{
  "event_type": "message_received",
  "timestamp": "2024-01-15T10:30:00Z",
  "session": {
    "id": 123,
    "uuid": "aYIEU268MM",
    "name": "Visitor #45",
    "started_at": "2024-01-15T10:25:00Z",
    "visitor_name": "John Doe",
    "visitor_country": "US",
    "visitor_lang": "en_US"
  },
  "message": {
    "id": 456,
    "body": "Hello, I need help",
    "author_id": 789,
    "author_name": "John Doe",
    "author_type": "visitor",
    "created_at": "2024-01-15T10:30:00Z"
  },
  "channel": {
    "id": 1,
    "name": "Website Support"
  },
  "metadata": {
    "odoo_base_url": "https://your-odoo.com",
    "callback_url": "https://your-odoo.com/im_livechat_n8n/webhook",
    "api_key_header": "X-API-Key"
  }
}
```

### 入站 Webhook（n8n → Odoo）

**端點**：`POST /im_livechat_n8n/webhook`

**Headers**：

| Header | 值 | 必填 |
|--------|------|------|
| `Content-Type` | `application/json` | 是 |
| `X-API-Key` | `your-api-key` | 是 |

**請求負載**：

```json
{
  "action": "send_message",
  "session_uuid": "aYIEU268MM",
  "message": {
    "body": "感謝您的來訊！我是 AI 助手，請問有什麼可以幫忙的？",
    "author_name": "AI Assistant"
  }
}
```

**回應狀態碼**：

| 狀態碼 | 說明 |
|--------|------|
| `200` | 訊息發送成功 |
| `400` | 無效的請求負載（缺少欄位、UUID 格式錯誤、訊息過大） |
| `401` | 無效或缺少 API 金鑰 |
| `404` | 找不到會話 |
| `500` | 伺服器錯誤 |

**驗證規則**：

- Session UUID 必須符合 `^[A-Za-z0-9_-]{6,50}$` 格式
- 訊息主體為必填欄位，且不得超過 10KB
- API 金鑰必須與已設定的頻道金鑰匹配

---

## Webhook 記錄管理

### 查看記錄

- **從頻道進入**：網站 > 即時聊天 > 頻道 > [選擇頻道] > 「View Webhook Logs」按鈕
- **直接存取**：網站 > 設定 > Webhook Logs

### 記錄欄位

| 欄位 | 說明 |
|------|------|
| 時間戳記 | Webhook 觸發時間 |
| 方向 | 出站（Odoo → N8N）/ 入站（N8N → Odoo） |
| 狀態 | 成功 / 失敗 / 逾時 |
| 回應時間 | 回應時間（毫秒） |
| HTTP 狀態碼 | HTTP 回應碼 |
| 請求負載 | 請求 JSON 內容（供除錯使用） |
| 回應負載 | 回應 JSON 內容（供除錯使用） |
| 錯誤訊息 | 失敗時的錯誤訊息 |

### 記錄保留政策

排程動作會自動清除超過 **30 天** 的 webhook 記錄，防止記錄表無限增長。

---

## 錯誤處理

### 出站 Webhook（Odoo → n8n）

| 項目 | 說明 |
|------|------|
| **重試邏輯** | 3 次自動重試，搭配指數退避機制（1s、2s、4s） |
| **逾時設定** | 每次嘗試 10 秒 |
| **非阻塞** | Webhook 在背景守護線程中發送，不影響聊天回應速度 |
| **記錄** | 所有失敗均記錄錯誤訊息 |

### 入站 Webhook（n8n → Odoo）

| 項目 | 說明 |
|------|------|
| **驗證** | 嚴格的輸入驗證（UUID 格式、訊息大小、必填欄位） |
| **安全性** | 所有請求均需 API 金鑰認證 |
| **錯誤回應** | 明確的錯誤訊息搭配適當的 HTTP 狀態碼 |

---

## 疑難排解

### Webhook 未觸發

1. 確認已勾選「Enable N8N Integration」
2. 確認 webhook URL 設定正確
3. 使用「Test Webhook Connection」按鈕測試連線
4. 檢查 webhook 記錄中的錯誤訊息

### n8n 未收到 Webhook

1. 確認 n8n webhook URL 可從 Odoo 伺服器存取
2. 檢查防火牆/網路規則
3. 檢視 Odoo 日誌：`odoo.addons.im_livechat_n8n`
4. 先使用 n8n 公有雲端實例進行測試

### Odoo 未收到回覆

1. 確認 Odoo 與 n8n 之間的 API 金鑰一致
2. 檢查回覆中的 session UUID 是否正確
3. 確認已在請求中包含 `X-API-Key` header
4. 檢視 Odoo 中的入站 webhook 記錄

### 訊息未顯示在聊天中

1. 確認會話仍處於活動狀態
2. 檢查 session UUID 格式是否有效
3. 檢視入站 webhook 記錄中的錯誤
4. 確認訊息主體不為空

---

## 效能考量

| 項目 | 規格 |
|------|------|
| Webhook 發送方式 | 非同步（非阻塞守護線程） |
| 最大訊息大小 | 10KB |
| Webhook 逾時 | 10 秒 |
| 重試次數 | 3 次（指數退避） |
| 記錄清理 | 自動（30 天保留期） |

---

## 授權條款

本模組採用 [LGPL-3.0](https://www.gnu.org/licenses/lgpl-3.0.html) 授權條款發布。

---

## 技術支援

如有問題或功能建議，請至：
https://github.com/WOOWTECH/im_livechat_n8n/issues

---

## 致謝

由 **[WOOWTECH](https://github.com/WOOWTECH)** 開發維護

---

<div align="center">

**[English](README.md)** | 繁體中文

</div>
