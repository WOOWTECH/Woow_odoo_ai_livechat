# 設計文件：im_livechat_n8n → im_livechat_ai 重構計畫

**日期**: 2026-05-12
**作者**: WOOWTECH
**版本**: 1.0

## 1. 目標

將現有的 `im_livechat_n8n` 模組完全取代，從依賴 n8n webhook 外部編排的架構，重構為 Odoo 模組直接呼叫 OpenAI 相容 API（`/v1/chat/completions`）的架構。

**支援的 LLM 提供商**：任何符合 OpenAI API 格式的服務（OpenAI、MiniMax、DeepSeek、Ollama、vLLM、Azure OpenAI 等）。

## 2. 架構變更

### 2.1 現有架構（n8n Webhook）

```
訪客訊息 → Odoo discuss.channel._notify_thread()
    → _should_trigger_n8n_webhook()
    → _trigger_n8n_webhook() (背景執行緒)
        → HTTP POST → n8n webhook URL
        → n8n 呼叫 LLM
        → n8n 回呼 POST /im_livechat_n8n/webhook
            → _create_bot_message() 發送回覆
```

### 2.2 新架構（直連 LLM API）

```
訪客訊息 → Odoo discuss.channel._notify_thread()
    → _should_trigger_ai_response()
    → _trigger_ai_response() (背景執行緒)
        → _build_llm_messages() 組裝對話歷史
        → _call_llm_api() POST /v1/chat/completions
        → _create_bot_message() 發送回覆
        → _create_api_log() 記錄日誌
        → 失敗時重試 3 次，最終發送錯誤提示
```

### 2.3 關鍵差異

| 項目 | 現有 n8n | 新 AI 直連 |
|------|----------|-----------|
| 訊息流向 | Odoo → n8n → LLM → n8n → Odoo | Odoo → LLM → Odoo |
| 回覆機制 | n8n 回呼 webhook controller | 背景執行緒直接寫入 |
| Controller | 需要（接收 n8n 回呼） | 不需要（移除） |
| 上下文管理 | n8n 負責 | Odoo 從 DB 取歷史 |
| 延遲 | 較高（多一跳） | 較低（直連） |
| 部署依賴 | 需要 n8n 實例 | 只需 API Key |

## 3. 模組結構

### 3.1 模組名稱：`im_livechat_ai`

```
im_livechat_ai/
├── __init__.py
├── __manifest__.py
├── models/
│   ├── __init__.py
│   ├── im_livechat_channel.py    # 頻道 AI 設定 + LLM API 呼叫邏輯
│   ├── discuss_channel.py         # 訊息攔截 → 觸發 AI 回覆 + 歷史組裝
│   └── llm_api_log.py            # API 呼叫日誌模型
├── security/
│   └── ir.model.access.csv
├── views/
│   ├── im_livechat_channel_views.xml   # 頻道設定 AI 分頁
│   └── llm_api_log_views.xml           # 日誌列表/表單
├── data/
│   └── ai_data.xml                     # 預設 Bot Partner、Cron
└── i18n/
    └── zh_TW.po                        # 繁體中文翻譯
```

### 3.2 移除的部分

- `controllers/` 目錄（整個移除，不再需要 webhook controller）
- `controllers/__init__.py`
- `controllers/webhook.py`（246 行）
- 所有 n8n 相關命名

## 4. 資料模型設計

### 4.1 im_livechat.channel 擴展欄位

| 欄位名 | 類型 | 預設值 | 說明 |
|--------|------|--------|------|
| `ai_enabled` | Boolean | False | 啟用 AI 自動回覆 |
| `ai_api_base_url` | Char | - | API Base URL（例如 `https://api.openai.com/v1`） |
| `ai_api_key` | Char | - | API Key |
| `ai_model` | Char | - | Model 名稱（例如 `gpt-4o`） |
| `ai_system_prompt` | Text | - | System Prompt（角色設定） |
| `ai_max_history` | Integer | 50 | 最大歷史訊息數 |
| `ai_max_retries` | Integer | 3 | 最大重試次數 |
| `ai_retry_delay` | Integer | 2 | 重試間隔（秒） |
| `ai_error_message` | Text | "目前客服忙碌中..." | 錯誤時回覆訪客的訊息 |
| `ai_bot_partner_id` | Many2one → res.partner | - | 頻道專屬 Bot Partner |
| `ai_bot_name` | Char | "AI Assistant" | Bot 顯示名稱 |
| `ai_temperature` | Float | 0.7 | LLM temperature 參數 |
| `ai_max_tokens` | Integer | 1024 | 回覆最大 token 數 |

### 4.2 llm.api.log 模型

| 欄位名 | 類型 | 說明 |
|--------|------|------|
| `timestamp` | Datetime | 呼叫時間（required, indexed） |
| `livechat_channel_id` | Many2one → im_livechat.channel | 所屬頻道 |
| `discuss_channel_id` | Many2one → discuss.channel | 對話頻道 |
| `model` | Char | 使用的 model 名稱 |
| `status` | Selection(success/error/retry) | 呼叫狀態 |
| `request_payload` | Text | 送出的 messages 陣列（JSON） |
| `response_payload` | Text | API 回應內容（JSON） |
| `prompt_tokens` | Integer | Prompt token 數 |
| `completion_tokens` | Integer | Completion token 數 |
| `total_tokens` | Integer | 總 token 數 |
| `response_time` | Float | 回應耗時（秒） |
| `error_message` | Text | 錯誤訊息 |
| `retry_count` | Integer | 重試次數 |

## 5. 核心邏輯

### 5.1 訊息攔截（discuss_channel.py）

```python
def _notify_thread(self, message, msg_vals=False, **kwargs):
    result = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)
    if self._should_trigger_ai_response(message):
        self.livechat_channel_id._trigger_ai_response(self, message)
    return result

def _should_trigger_ai_response(self, message):
    # 1. channel_type == 'livechat'
    # 2. livechat_channel_id 存在
    # 3. ai_enabled == True
    # 4. 訊息是訪客發的（非 bot、非 operator）

def _is_visitor_message(self, message):
    # 排除 bot partner（頻道專屬的 ai_bot_partner_id）
    # 排除 OdooBot (base.partner_root)
    # 判定為訪客：author_guest_id / 無 author_id / author 無 user_ids

def _build_llm_messages(self, livechat_channel):
    # 1. 取最近 ai_max_history 條訊息
    # 2. 建構 messages 陣列：
    #    - bot partner 的訊息 → role: "assistant"
    #    - 其他人的訊息 → role: "user"
    # 3. 最前面插入 {"role": "system", "content": ai_system_prompt}
    # 4. 回傳 messages 陣列
```

### 5.2 LLM API 呼叫（im_livechat_channel.py）

```python
def _trigger_ai_response(self, discuss_channel, message):
    # 在背景執行緒中執行
    thread = threading.Thread(
        target=self._process_ai_response,
        args=(discuss_channel.id,),
        daemon=True
    )
    thread.start()

def _process_ai_response(self, discuss_channel_id):
    # 1. 開新的 db cursor
    # 2. 組裝 messages = discuss_channel._build_llm_messages(self)
    # 3. for attempt in range(ai_max_retries):
    #        try:
    #            response = _call_llm_api(messages)
    #            _create_bot_message(discuss_channel, response)
    #            _create_api_log(success)
    #            return
    #        except:
    #            _create_api_log(retry)
    #            time.sleep(ai_retry_delay)
    # 4. 全部失敗：_create_bot_message(discuss_channel, ai_error_message)
    # 5. _create_api_log(error)

def _call_llm_api(self, messages):
    # POST {ai_api_base_url}/chat/completions
    # Headers: Authorization: Bearer {ai_api_key}, Content-Type: application/json
    # Body: {model, messages, temperature, max_tokens}
    # Timeout: 60s
    # 回傳: response JSON（含 choices[0].message.content, usage）
```

### 5.3 Bot Partner 管理

```python
def _get_or_create_bot_partner(self):
    # 如果 ai_bot_partner_id 已存在，更新名稱（如有變更）
    # 如果不存在，建立新的 res.partner，設定名稱為 ai_bot_name
    # 回傳 partner 記錄
```

## 6. UI 設計

### 6.1 頻道設定 — AI Integration 分頁

```
[AI Integration 分頁]
┌─────────────────────────────────────────────┐
│ ☑ 啟用 AI 自動回覆                          │
│                                             │
│ ── AI 連線設定 ──                            │
│ API Base URL: [https://api.openai.com/v1  ] │
│ API Key:      [sk-xxxx...          ] 🔒     │
│ Model:        [gpt-4o                     ] │
│                                             │
│ ── Bot 設定 ──                               │
│ Bot 名稱:     [AI Assistant               ] │
│ Bot Partner:  [AI Assistant (自動建立)     ] │
│                                             │
│ ── System Prompt ──                         │
│ ┌─────────────────────────────────────────┐ │
│ │ 你是一個友善的客服助手...                  │ │
│ └─────────────────────────────────────────┘ │
│                                             │
│ ── 進階設定 ──                               │
│ 歷史訊息數:   [50    ]                      │
│ Temperature:  [0.7   ]                      │
│ Max Tokens:   [1024  ]                      │
│ 重試次數:     [3     ]                      │
│ 重試間隔(秒): [2     ]                      │
│ 錯誤提示訊息: [目前客服忙碌中...            ] │
│                                             │
│ [🔗 測試 API 連線]  [📋 查看 API 日誌]       │
└─────────────────────────────────────────────┘
```

## 7. 安全性考量

- **API Key 儲存**：使用 Odoo 標準欄位儲存，前端顯示為 password widget
- **權限控制**：沿用現有 livechat user/manager 權限分層
  - User：只能讀取 API 日誌
  - Manager：完整 CRUD + 頻道 AI 設定
- **Request Payload 日誌**：可選擇是否記錄完整 payload（含使用者訊息），需注意隱私

## 8. 測試計畫

需要重寫所有測試，涵蓋：

1. **im_livechat_channel 測試**
   - AI 欄位驗證（URL 格式、必填檢查）
   - Bot Partner 自動建立/更新
   - API 呼叫 mock 測試
   - 重試邏輯測試
   - 錯誤處理測試

2. **discuss_channel 測試**
   - 訪客訊息偵測
   - Bot 訊息防迴圈
   - 對話歷史組裝
   - messages 陣列角色判定

3. **llm_api_log 測試**
   - 日誌建立
   - 日誌清理（30 天）
   - Token 統計

4. **整合測試**
   - 完整流程：訪客發訊息 → AI 回覆
   - 多頻道隔離
   - 錯誤降級

## 9. 遷移策略

這是**全新部署**，不需要資料遷移：
- 建立全新的 `im_livechat_ai` 模組
- 舊的 `im_livechat_n8n` 模組可手動卸載
- 不提供自動遷移路徑

## 10. 實作順序

1. **Phase 1**：建立模組骨架（`__manifest__.py`、`__init__.py`、目錄結構）
2. **Phase 2**：實作資料模型（`im_livechat_channel.py` 欄位、`llm_api_log.py`）
3. **Phase 3**：實作核心邏輯（LLM API 呼叫、對話歷史組裝、重試機制）
4. **Phase 4**：實作 UI（XML views、security CSV）
5. **Phase 5**：實作 Bot Partner 管理 + data XML
6. **Phase 6**：繁體中文翻譯
7. **Phase 7**：測試套件
8. **Phase 8**：Code Review + 修正
