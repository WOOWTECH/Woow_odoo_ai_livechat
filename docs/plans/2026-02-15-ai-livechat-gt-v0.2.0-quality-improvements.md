# PRD: ai_livechat_gt v0.2.0 品質改善與問題修復

## 文件資訊

| 項目 | 內容 |
|------|------|
| 模組名稱 | ai_livechat_gt |
| 當前版本 | 0.1.0 |
| 目標版本 | 0.2.0 |
| 作者 | GT Apps |
| 日期 | 2026-02-15 |
| 狀態 | 已完成實作 |

---

## 1. 背景與目標

### 1.1 背景

ai_livechat_gt 是 Odoo 18 的 AI Livechat 整合模組，將 AI 聊天機器人整合到 Odoo 的即時通訊系統中。經過完整的 code review 後，發現 22 個需要修復的問題：

- **4 個嚴重問題 (Critical)**
- **14 個重要問題 (Important)**
- **4 個建議改善項目 (Suggestion)**

### 1.2 目標

- 修復所有已識別的安全性、穩定性和效能問題
- 提升程式碼品質和可維護性
- 完善文件和錯誤處理機制

### 1.3 成功標準

- [x] 所有 Critical 和 Important 問題修復完成
- [x] 模組可在 Odoo 18 正常安裝和運行
- [x] API 測試驗證所有欄位和功能正常
- [x] 無新增回歸問題

---

## 2. 問題分析與修復方案

### 2.1 Critical 問題 (4 項)

#### 2.1.1 IndexError Bug - rfind 返回 -1

**檔案:** `models/discuss_channel.py:37-38`

**問題:** 當 `rfind()` 找不到 AI operator 名稱時返回 -1，導致字串切片錯誤截斷。

**修復方案:**
```python
ai_name_position = self.name.rfind(ai_operator_name)
if ai_name_position != -1:
    new_name = self.name[:ai_name_position].strip() + " " + human_operator_name
else:
    new_name = self.name.strip() + " " + human_operator_name
```

**狀態:** ✅ 已完成

---

#### 2.1.2 AttributeError - livechat_operator_id null check

**檔案:** `models/discuss_channel.py:35`

**問題:** 存取 `self.livechat_operator_id.name` 前未檢查是否為空。

**修復方案:**
```python
if self.livechat_operator_id:
    ai_operator_name = self.livechat_operator_id.name
    # ... 處理名稱替換
```

**狀態:** ✅ 已完成

---

#### 2.1.3 缺少 Security 檔案

**問題:** 模組未包含 `security/ir.model.access.csv` 檔案。

**修復方案:** 建立空的 security 檔案（模組只繼承現有模型，無需額外權限定義）。

**狀態:** ✅ 已完成

---

#### 2.1.4 sudo() 無例外處理

**檔案:** `models/ai_thread.py:19`

**問題:** `sudo()` 呼叫可能拋出例外但未捕獲。

**修復方案:**
```python
try:
    human_operator = self.discuss_channel_id.sudo()._ai_forward_to_human_operator()
    # ...
except Exception as e:
    _logger.exception("Error forwarding to human operator: %s", str(e))
    return {'status': False, 'error': _("An error occurred...")}
```

**狀態:** ✅ 已完成

---

### 2.2 Important 問題 (14 項)

| # | 問題 | 檔案 | 修復方案 | 狀態 |
|---|------|------|----------|------|
| 1 | 錯誤處理不完整 | ai_thread.py | 新增 try/except 和 logging | ✅ |
| 2 | 缺少 logging | discuss_channel.py | 新增 `_logger` 記錄 | ✅ |
| 3 | 缺少 docstrings | im_livechat_channel.py | 為所有方法新增 docstrings | ✅ |
| 4 | computed field 效能 | im_livechat_channel.py | 優化 ai_assistant_id 計算 | ✅ |
| 5 | 缺少 help text | im_livechat_channel.py | 新增欄位說明 | ✅ |
| 6 | channel name 未驗證 | discuss_channel.py | 新增長度限制 (256 chars) | ✅ |
| 7 | 缺少 README | / | 建立安裝說明文件 | ✅ |
| 8 | race condition | discuss_channel.py | try/except 包裝操作 | ✅ |
| 9 | N+1 query | discuss_channel_member.py | 新增 prefetch 優化 | ✅ |
| 10 | 重複 with_context | im_livechat_channel.py | 優化呼叫方式 | ✅ |
| 11 | 魔術數字 | mail_ai.py | 抽取為常數 | ✅ |
| 12 | 欄位覆寫方式 | im_livechat_channel.py | 改善繼承方式 | ✅ |
| 13 | assets 依賴 | __manifest__.py | 新增註解說明 | ✅ |
| 14 | 缺少 security 檔案引用 | __manifest__.py | 加入 data 列表 | ✅ |

---

### 2.3 Suggestion 改善 (4 項)

| # | 問題 | 檔案 | 修復方案 | 狀態 |
|---|------|------|----------|------|
| 1 | 未使用的 import | im_livechat_channel.py | 檢查後保留（實際有使用） | ✅ |
| 2 | 缺少 inline comments | discuss_channel.py | 新增詳細註解 | ✅ |
| 3 | 魔術數字 2 | mail_ai.py | 定義 `_PRIVATE_LIVECHAT_MAX_MEMBERS` | ✅ |
| 4 | N+1 prefetch | discuss_channel_member.py | 新增 `mapped()` prefetch | ✅ |

---

## 3. 架構變更

### 3.1 新增檔案

```
ai_livechat_gt/
├── README.md                    # 新增：安裝與設定說明
└── security/
    └── ir.model.access.csv      # 新增：安全性存取規則
```

### 3.2 修改檔案

| 檔案 | 變更類型 | 說明 |
|------|----------|------|
| `__manifest__.py` | 修改 | 版本號 0.2.0、新增 security 引用 |
| `models/ai_thread.py` | 重構 | 新增錯誤處理和 logging |
| `models/discuss_channel.py` | 重構 | 修復 bugs、新增 logging 和驗證 |
| `models/discuss_channel_member.py` | 優化 | 新增 prefetch 優化 |
| `models/im_livechat_channel.py` | 重構 | 新增 docstrings 和 help text |
| `models/mail_ai.py` | 重構 | 抽取常數、改善程式結構 |

---

## 4. 測試驗證

### 4.1 部署測試

| 測試項目 | 環境 | 結果 |
|----------|------|------|
| 模組安裝 | Odoo 18 / woowtech DB | ✅ 成功 |
| 依賴檢查 | ai_mail_gt, im_livechat | ✅ 已安裝 |
| 資料庫遷移 | im_livechat.channel 新欄位 | ✅ 正常 |

### 4.2 功能測試

| 測試項目 | API 端點 | 結果 |
|----------|----------|------|
| 讀取 livechat channel | search_read | ✅ 正常 |
| 讀取 AI assistant | search_read | ✅ 正常 |
| 設定 ai_assistant_id | write | ✅ 正常 |
| ai_context_id 計算 | computed field | ✅ 自動繼承 |
| available_operator_ids | computed field | ✅ 包含 AI user |

### 4.3 測試結果

```json
// 測試：分配 AI Assistant 到 Livechat Channel
// 請求：write([1], {"ai_assistant_id": 2})
// 回應：true

// 驗證結果：
{
  "id": 1,
  "name": "YourWebsite.com",
  "ai_assistant_id": [2, "Livechat Assistant"],
  "ai_context_id": [2, "Livechat context"],
  "available_operator_ids": [13]
}
```

---

## 5. 版本資訊

### 5.1 變更日誌

```
## [0.2.0] - 2026-02-15

### Fixed
- Critical: IndexError bug in channel name manipulation (rfind -1 handling)
- Critical: AttributeError when livechat_operator_id is not set
- Critical: Missing security/ir.model.access.csv file
- Critical: Unhandled exceptions in sudo() calls

### Added
- Comprehensive error handling with try/except blocks
- Logging throughout discuss_channel.py and ai_thread.py
- README.md with installation and configuration documentation
- Help text for ai_assistant_id field
- Docstrings for all public methods
- Inline comments for complex logic

### Changed
- Optimized N+1 queries with prefetch in discuss_channel_member.py
- Extracted magic numbers to class constants
- Improved channel name validation with length limits
- Enhanced AI context computation for better clarity

### Security
- Added security/ir.model.access.csv (empty for inherited models)
- Removed internal operator ID from API response
```

---

## 6. 附錄

### 6.1 測試帳號

| 項目 | 值 |
|------|------|
| URL | http://localhost:8069 |
| Database | woowtech |
| Email | woowtech@designsmart.com.tw |
| Password | test123 |

### 6.2 相關連結

- Live Demo: https://ai-demo-18.gt-apps.top
- Documentation: https://ai-docs-18.gt-apps.top
- Support: gt.apps.odoo@gmail.com
