# 實作計畫：im_livechat_ai 模組

**日期**: 2026-05-12
**對應設計文件**: `2026-05-12-n8n-to-openai-api-migration-design.md`

## Phase 1：模組骨架

**目標**：建立新模組的基本結構，確保可安裝。

### 步驟：
- [ ] 1.1 建立 `im_livechat_ai/` 目錄結構
- [ ] 1.2 撰寫 `__manifest__.py`（更新名稱、描述、依賴）
- [ ] 1.3 撰寫所有 `__init__.py`（模組入口、models 入口）
- [ ] 1.4 建立空的模型檔案骨架

**驗證**：模組目錄結構完整，Python import 鏈正確。

---

## Phase 2：資料模型

**目標**：實作所有 Odoo model 欄位定義。

### 步驟：
- [ ] 2.1 `im_livechat_channel.py` — 新增所有 AI 設定欄位（14 個欄位）
  - ai_enabled, ai_api_base_url, ai_api_key, ai_model
  - ai_system_prompt, ai_max_history, ai_temperature, ai_max_tokens
  - ai_max_retries, ai_retry_delay, ai_error_message
  - ai_bot_partner_id, ai_bot_name
  - 欄位驗證 constraint
- [ ] 2.2 `llm_api_log.py` — 建立日誌模型（13 個欄位）
  - timestamp, livechat_channel_id, discuss_channel_id, model
  - status, request_payload, response_payload
  - prompt_tokens, completion_tokens, total_tokens
  - response_time, error_message, retry_count
  - _cleanup_old_logs() 方法
- [ ] 2.3 `security/ir.model.access.csv` — 權限規則

**驗證**：模型定義正確，欄位類型和預設值正確。

---

## Phase 3：核心邏輯

**目標**：實作 LLM API 呼叫、對話歷史組裝、重試機制。

### 步驟：
- [ ] 3.1 `discuss_channel.py` — 訊息攔截
  - _notify_thread() override
  - _should_trigger_ai_response()
  - _is_visitor_message()（排除 bot、operator）
  - _build_llm_messages()（歷史組裝 + system prompt）
- [ ] 3.2 `im_livechat_channel.py` — LLM API 呼叫
  - _trigger_ai_response()（背景執行緒啟動）
  - _process_ai_response()（重試迴圈主邏輯）
  - _call_llm_api()（HTTP POST /v1/chat/completions）
  - _create_bot_message()（發送回覆到聊天室）
  - _create_api_log()（記錄日誌，獨立 cursor）
- [ ] 3.3 Bot Partner 管理
  - _get_or_create_bot_partner()

**驗證**：核心邏輯完整，錯誤處理正確，防迴圈機制有效。

---

## Phase 4：UI 與安全性

**目標**：建立設定介面和日誌介面。

### 步驟：
- [ ] 4.1 `views/im_livechat_channel_views.xml`
  - AI Integration 分頁（繼承原始 form view）
  - 連線設定區塊
  - Bot 設定區塊
  - System Prompt 區塊
  - 進階設定區塊
  - 操作按鈕（測試連線、查看日誌）
- [ ] 4.2 `views/llm_api_log_views.xml`
  - List view（時間、頻道、model、狀態、tokens、耗時）
  - Form view（完整詳情 + request/response payload）
  - Search view（篩選：狀態、日期、頻道）
  - Action + 選單項目
- [ ] 4.3 更新 `security/ir.model.access.csv`

**驗證**：UI 可正常顯示，欄位可編輯，日誌可查看。

---

## Phase 5：Data 與 Bot Partner

**目標**：建立預設資料。

### 步驟：
- [ ] 5.1 `data/ai_data.xml`
  - 預設 AI Bot Partner（作為 fallback）
  - Cron job：每日清理舊日誌

**驗證**：模組安裝時自動建立預設資料。

---

## Phase 6：繁體中文翻譯

### 步驟：
- [ ] 6.1 建立 `i18n/zh_TW.po`
  - 翻譯所有模型名稱、欄位標籤、help text
  - 翻譯所有 UI 字串

---

## Phase 7：測試套件

### 步驟：
- [ ] 7.1 `tests/test_im_livechat_ai.py` — 基礎測試
  - 欄位驗證測試
  - Bot Partner 建立/更新測試
  - API 呼叫 mock 測試（成功/失敗/重試）
  - 日誌建立測試
  - 日誌清理測試
- [ ] 7.2 `tests/test_discuss_channel.py` — 對話測試
  - 訪客偵測測試
  - Bot 防迴圈測試
  - 歷史訊息組裝測試
  - messages 陣列格式驗證
- [ ] 7.3 `tests/test_integration.py` — 整合測試
  - 完整流程測試（mock API）
  - 多頻道隔離測試
  - 錯誤降級測試

---

## Phase 8：Code Review + 修正

### 步驟：
- [ ] 8.1 Code review 所有檔案
- [ ] 8.2 修正發現的問題
- [ ] 8.3 最終驗證
