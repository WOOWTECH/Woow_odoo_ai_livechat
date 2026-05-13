# im_livechat_ai E2E 測試計畫：15 個企業場景

## 環境

- Odoo 18.0 on Podman (port 9094)
- MiniMax API (MiniMax-Text-01)
- Playwright CLI for browser automation
- DB direct query for payload verification

## 測試工具

| 層面 | 工具 |
|------|------|
| 前端 UI | Playwright CLI (`npx playwright-cli`) |
| 瀏覽器對話 | 獨立 session (`-s visitor`, `-s visitor2`) |
| 後端驗證 | `podman exec odoo-ailivechat-db psql` |
| API 層 | Odoo JSON-RPC (`/web/dataset/call_kw`) |
| 日誌 | `podman logs odoo-ailivechat-web` |

---

## A 類：真實多輪業務對話（驗證上下文記憶）

### 場景 1：客服問答鏈（5 輪連續）
- **操作**：訪客依序問 5 個問題（商品、價格、配送、退貨、客訴）
- **驗證**：DB 中第 5 輪的 request_payload 包含前 4 輪的 user+assistant
- **方法**：Playwright visitor session + DB query

### 場景 2：上下文記憶測試
- **操作**：訪客說「我叫小明」→ 第二輪問「我剛才說我叫什麼名字？」
- **驗證**：AI 回覆包含「小明」；DB payload 有完整歷史
- **方法**：Playwright + DB + 回覆內容解析

### 場景 3：多語言切換
- **操作**：同一對話中依序用中文→英文→日文發訊息
- **驗證**：AI 回覆語言對應（或至少不 crash）；payload 正確
- **方法**：Playwright + DB

---

## B 類：併發與壓力（驗證 serialization retry）

### 場景 4：快速連續訊息（3 秒 3 條）
- **操作**：訪客在 3 秒內連續發 3 條訊息
- **驗證**：3 條都收到 AI 回覆；無 error log；API log 有 3 筆 success
- **方法**：Playwright evaluate + setTimeout + DB count

### 場景 5：雙訪客同時對話
- **操作**：兩個獨立 browser session 分別開啟 livechat 並同時發訊息
- **驗證**：兩個 discuss_channel 各自收到 AI 回覆；無 serialization error
- **方法**：兩個 Playwright session + DB query by discuss_channel_id

### 場景 6：超長訊息（2000 字元）
- **操作**：發送重複文字組成的 2000 字元訊息
- **驗證**：API 正常處理不 timeout；回覆正常；payload 完整記錄
- **方法**：Playwright evaluate + DB payload length check

---

## C 類：邊緣觸發條件（驗證 _should_trigger/_is_visitor）

### 場景 7：Bot 迴圈防護
- **操作**：發送一條訪客訊息，等 AI 回覆後，DB 查 bot 訊息的 author_id
- **驗證**：bot author_id == ai_bot_partner_id；該訊息不再觸發新 AI 回覆
- **方法**：DB query on mail_message + llm_api_log count

### 場景 8：管理員訊息不觸發
- **操作**：管理員從 admin session 在 Discuss 中對同一 livechat 對話發訊息
- **驗證**：不產生新的 AI 回覆（API log count 不增加）
- **方法**：Playwright admin session + DB log count before/after

### 場景 9：AI 停用測試
- **操作**：後台關閉 ai_enabled → 訪客發訊息 → 再開啟 ai_enabled
- **驗證**：停用期間無 AI 回覆；啟用後恢復正常
- **方法**：Playwright admin toggle + visitor message + DB

---

## D 類：錯誤處理與恢復（驗證 retry + error_message）

### 場景 10：無效 API Key
- **操作**：修改 ai_api_key 為 `sk-invalid-key-12345`，訪客發訊息
- **驗證**：訪客收到 ai_error_message；API log 有 error + retry 記錄
- **方法**：DB update → Playwright visitor → DB log check → restore key

### 場景 11：無效 Model 名稱
- **操作**：修改 ai_model 為 `nonexistent-model-xyz`，訪客發訊息
- **驗證**：訪客收到錯誤訊息；log 記錄 error status
- **方法**：DB update → Playwright visitor → DB check → restore

### 場景 12：API URL 不可達
- **操作**：修改 ai_api_base_url 為 `https://localhost:1/v1`
- **驗證**：訪客收到錯誤訊息；log 有 ConnectionError；retry 次數正確
- **方法**：DB update → Playwright visitor → wait longer → DB check → restore

---

## E 類：系統功能完整性（UI + Log + 設定）

### 場景 13：System Prompt 語言控制
- **操作**：修改 system_prompt 為「Please reply in English only」
- **驗證**：訪客用中文問，AI 用英文回答
- **方法**：DB update prompt → Playwright visitor → verify reply language

### 場景 14：Bot Name 顯示
- **操作**：修改 ai_bot_name 為「智慧客服小幫手」
- **驗證**：AI 回覆在聊天室中顯示為「智慧客服小幫手」
- **方法**：Playwright admin update → visitor dialog → snapshot check

### 場景 15：API Log 總覽驗證
- **操作**：全部場景結束後，匯出所有 API log
- **驗證**：每筆記錄的 timestamp/model/status/tokens/response_time 都有值
- **方法**：DB comprehensive query + report

---

## 執行順序

先執行 A/E 類（正常路徑），再 B 類（壓力），再 C 類（邊緣），最後 D 類（錯誤）。
D 類需要修改設定，最後執行以免影響其他測試。

## 預計產出

- 每個場景的 Playwright 截圖或 snapshot
- DB 查詢結果
- 最終綜合報告表格
