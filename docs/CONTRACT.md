# AI products contract, version 1

- **Version:** v1, 2026-11-25.
- **Products:**
  - Discuss AI (`woow_ai_contact`);
  - Project AI (`woow_ai_task`);
  - Live Chat AI (`im_livechat_ai`).
- **Status:** normative. "Must" and "never" are requirements; "may" is a permitted choice.

The three products are separate. Each keeps its own runtime core and ships on its own, and none of them uses another one's code. This contract is what makes them configured and run the same way. Every repository carries a byte-identical copy of this file as `docs/CONTRACT.md`, and the contract package as `<module>/tests/contract/` (section 6). A product may lag a version: its README names the version it implements.

## 1. Owner decisions this contract implements

- **Three cores, three downloads.** Live Chat AI is upgraded in place. It is not merged into another core and not retired.
- **n8n or LiteLLM only.** The stored backend key `openai` stays, labelled 「LiteLLM（OpenAI 相容）」.
- **A LiteLLM connection is always 「可給客戶使用」**, the same in the three products.
- **Two layers.** 「AI 連線」 (connection) is for system administrators only. 「AI 助理」 (assistant) uses a connection.
- **Live Chat (Q12 A).**
  - A system administrator ticks 「可用在 Live Chat」 on an assistant. Live Chat managers can pick only those assistants.
  - The connection is not forced to be safe for customers; the AI tab warns instead.
- **Every message gets an answer.**
  - Replies are always queued on the server.
  - Every connection has a test and a readiness check.
- **Usage, not billing.**
  - Usage counts replies, plus tokens when the backend reports them.
  - There is no billing logic and no per-use charging. Limits live in the LiteLLM keys.
- **Discuss AI and portal customers (owner answer N3 A).** A portal customer gets no answer through a connection that is not 「可給客戶使用」. The upgrade ticks the n8n connections already shared with customers first.

## 2. Terms

- **Connection:** a record of the 「AI 連線」 model: the backend type, URL, key and call settings.
- **Assistant:** a record of the 「AI 助理」 model: the persona that answers, with its connection.
- **Conversation:** the `discuss.channel` an answer is given in.
- **Answer:** one backend call, retries included, and what it returns.
- **Blocked period:** the time from the first message the AI cannot answer until the AI answers again.

## 3. The contract

### 3.1 Fixed values per product

| | Discuss AI | Project AI | Live Chat AI |
|---|---|---|---|
| Repository | Gitea woow-paas/Woow_odoo_discuss_ai (GitHub mirror) | Gitea woow-paas/Woow_odoo_project_ai (GitHub mirror) | GitHub WOOWTECH/Woow_odoo_ai_livechat |
| Main module | `woow_ai_contact` | `woow_ai_task` | `im_livechat_ai` |
| Connection / assistant models | `woow.ai.connection` / `woow.ai.contact` | `woow.project.ai.connection` / `woow.project.ai.assistant` | `im_livechat.ai.connection` / `im_livechat.ai.assistant` |
| Job model | `woow.ai.job` | `woow.project.ai.job` | `im_livechat.ai.job` |
| Menu | 討論 > 配置 | 專案 > 配置 > 專案 AI | 線上客服 > 配置 |
| Where an assistant is chosen | on the assistant (allowed groups, shared users) | the project form, page 「AI 助理」 | the channel form, tab 「AI 助理」: only assistants marked 「可用在 Live Chat」 |
| Session id prefix | `woow-ai` | `woow-project-ai` | `woow-ai` |
| `surface.type` | `discuss` | `project_task` | `livechat` |
| n8n persona keys | `assistant`, plus the legacy `contact` (the assistant) | `assistant` | `assistant`, plus the legacy `contact` (the Live Chat channel) |
| Runner | cron dispatcher `_cron_run_jobs`; parameters `woow_ai_contact.job_runner` and `.max_concurrency` | cron dispatcher `_cron_run_jobs`; parameters `woow_ai_task.job_runner` and `.max_concurrency` | web thread plus a recovery cron; cap `im_livechat_ai.max_active_jobs` |
| LiteLLM streaming | yes | yes | no: whole replies |
| Push API | `/woow_ai/v1/push` (keys `wpk_…`) | `/woow_project_ai/v1/push` (keys `wpai_…`) | none |
| Signed file route | `/woow_ai/attachment/…` | `/woow_project_ai/attachment/…` | `/im_livechat_ai/attachment/…` |
| 「可給客戶使用」 gate | portal customers get no answer through an unsafe connection | the customer tab needs it | warning only |
| Identity block in the system message | yes (3.7) | yes (3.7) | none: visitors are mostly anonymous |
| Clickable links in replies | any http(s) host | any http(s) host | the site's own hosts only |
| Files in replies | attached from the download domains | attached from the download domains | text only |

**About the `contact` key.** It means different things in Discuss AI and Live Chat. New n8n workflows read `assistant`.

**Session ids.** Discuss AI and Live Chat share the prefix. This is safe because a `discuss.channel` id never repeats within one database.

### 3.2 Layers, roles, menus

**「AI 連線」.**
- Only `base.group_system` can read or write it.
- Runtime code reads it with sudo.

**「AI 助理」.**
- Only `base.group_system` can create or edit it.
- These fields are restricted to `base.group_system`: connection, system prompt, error message, push key, logs and runner information.
- Other readers see the other fields only:
  - **Discuss AI:** users and portal users the assistant allows.
  - **Project AI:** project users, so they can pick assistants.
  - **Live Chat:** Live Chat managers, and only the assistants marked 「可用在 Live Chat」.
- A product may also block the assistant list and form for people who are not administrators. This is allowed, not required.

**Reply jobs, call logs and usage.**
- Only `base.group_system` reads them.
- Jobs and call logs may also be deleted.

**Menus.**
- The app's 「配置」 menu holds five items, in this order, all for `base.group_system`: 「AI 連線」, 「AI 助理」, 「回覆工作」, 「呼叫紀錄」, 「AI 用量」.
- A parent menu named after the product is optional.
- Live Chat's channel tab is named 「AI 助理」.

**Archive, never delete.** Usage rows point to the assistant and the connection with `ondelete='restrict'`.

### 3.3 「AI 連線」 fields

Every product uses these field names. The zh_TW labels are in 3.11.

| Field | Rule |
|---|---|
| `name`, `description`, `active` | — |
| `backend_type` | `openai` (LiteLLM) or `n8n`. Default `openai`; required. |
| `endpoint_url` | https only. Plain http is allowed only for localhost, 127.0.0.1, ::1 and `*.svc.cluster.local`. LiteLLM: the base URL ending in /v1. n8n: the full production URL. |
| `api_key`, `api_key_set` | Write-only (see below). |
| `model` | LiteLLM: required. A connection without one is blocked, and Test Connection refuses it. n8n: not used. |
| `timeout_seconds` | The total time for one answer, retries included. |
| `openai_stream` | LiteLLM only. Discuss AI and Project AI stream by default: SSE with `stream_options.include_usage`, and the call fails after 30 s without a byte. Live Chat does not stream. |
| `max_retries` | 0 to 2, default 1 (see below). |
| `document_mode` | `file` (default) or `link`. |
| `download_domains`, `max_download_mb` | Products that attach reply files: the hosts files are downloaded from, and 1 to 100 MB per file (default 20). |
| `log_payload` | For debugging only. Stored content is redacted (no base64). It is erased when the flag is turned off. |
| `customer_safe` | Stored Boolean (see below). |
| `readiness`, `readiness_reason` | See below. |

**Timeout limits.**
- Discuss AI and Project AI allow 5 to 3600 s, default 300.
  - A new n8n connection defaults to 120 s, in `create()` and in the form.
  - An n8n answer waits at most 300 s.
- A product may cap the timeout lower and keep its own defaults. Live Chat allows 10 to 300 s, with defaults of 45 s (LiteLLM) and 120 s (n8n).
- Long n8n workflows should use an in-cluster URL, or answer `{"skip": true}` at once and push the reply.

**`customer_safe`.**
- LiteLLM is always True. Every path enforces it: a constraint, `create`, `write`, `copy` and the form's onchange.
- n8n is False until a system administrator ticks it. It is cleared when the backend type or the endpoint URL changes.

**`readiness`.**
- It is `ok` or `blocked`, and it is searchable.
- A connection is blocked when it is archived, has no URL, or (LiteLLM) has no key or no model.
- `readiness_reason` is made of fixed sentences, never a URL or a key.

**`api_key`.**
- It is a compute/inverse field over the SQL-only column `api_key_secret`. Only administrators may set it.
- LiteLLM: required, sent as `Authorization: Bearer`.
- n8n: an optional signing secret, which is never sent.
- The key is dropped when the scheme, host, port or backend type changes. A 「移除 API 金鑰」 button removes it. It is never copied.
- Logs say only that the key was updated or removed, and by whom (ids only).

**Retries.**
- A call is retried in two cases only:
  - the request was not sent (connection refused, unknown host, connect timeout);
  - the backend answered HTTP 429 or 503.
- The waits are 3 s, then 10 s. A retry never starts when its wait would reach the answer's deadline.
- A call is never retried after:
  - a read timeout;
  - a reset after the request was sent;
  - any other answer, 401, 502 and 504 included.
- Redirects are never followed.

**Test Connection** (administrators only).
- LiteLLM: `GET {endpoint_url}/models` with the Bearer key.
- n8n: the ping (3.6).
- Connect timeout 10 s, read timeout 15 s, no redirects.
- It shows the HTTP status and one fixed sentence, never the upstream body.

### 3.4 「AI 助理」 fields

- `name`, `description`, `image` (the bot partner follows it), `active`, `sequence`.
- `partner_id`: the bot partner, created automatically. It stays archived unless the product lists it.
- `connection_id`: required, administrators only.
- `system_prompt`: optional.
- `max_history`: 1 to 200, default 50.
- `error_message`:
  - Empty means the default of 3.8.
  - Live Chat may keep a per-channel override.
- A push key, where the product has push.
- The welcome message lives where the product shows it: Discuss AI on the assistant, Project AI on the project, Live Chat on the channel.
- There is no temperature, max tokens or language field: these are set on the LiteLLM side. Default texts follow the person's language.

### 3.5 How a turn runs

1. **Trigger.**
   - A person's comment is handled in the poster's transaction, under a savepoint.
   - A failure there is logged and never undoes the message.
   - The exception is PostgreSQL's concurrency errors (`PG_CONCURRENCY_EXCEPTIONS_TO_RETRY`). The trigger must re-raise them, so that Odoo retries the whole request. The savepoint flushes before it exists, so swallowing such an error leaves an aborted transaction and an HTTP 500.
2. **Gate before queuing.** When the AI cannot answer, no job is created and no backend is called.
   - The AI cannot answer when the assistant is archived, the connection is not ready, or a safety rule fails.
   - The conversation gets one fixed notice per blocked period. It is posted as a notification message, which the history never reads.
   - Staff are alerted in each product's own way.
3. **The job.**
   - States: `pending`, `running`, `done`, `failed`, `skipped`.
   - It is created in the poster's transaction, with a 1.5 s debounce, and runs at most 3 times.
   - Lost jobs are re-queued: pending and 30 s late, or running for 60 s with a free lock.
   - A job older than 30 minutes is given up, unless it no longer needs an answer.
     - The person then gets the error message.
     - Live Chat alerts its operators instead.
   - Job kinds are product-specific.
4. **One job per conversation at a time.**
   - An advisory lock in the product's own namespace, `crc32('<module>.job')`.
   - The newest job supersedes older ones, unless the surface uses the per-author policy.
5. **Runner.**
   - **Discuss AI and Project AI** use a cron dispatcher, `model._cron_run_jobs()`: priority 1, every minute, triggered for each job, with a thread fallback.
     - Each counts the active AI dispatchers of the database (`woow.ai.job` and `woow.project.ai.job`).
     - Each uses short passes when there are as many dispatchers as cron threads.
   - **Live Chat** keeps its web thread and recovery cron.
   - Whatever the runner:
     - replies run on the server;
     - a recovery path picks them up after a restart;
     - states and limits are the same;
     - administrators see runner information and warnings on the 「AI 助理」 form.
6. **No open transaction during a call.** The job commits before each HTTP call.
7. **Typing.** While the backend works, the bot shows "typing…". Live Chat should add this; it may come in a later version.

### 3.6 n8n wire format

**Request.**
- `POST endpoint_url` with `Content-Type: application/json`.
- The body is compact UTF-8 JSON (`ensure_ascii=False`, `separators=(',', ':')`). Those exact bytes are signed and sent.
- With a signing secret, two headers are added:
  - `X-Woow-Timestamp: <unix seconds>`
  - `X-Woow-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>`
- There is never an `Authorization` header.

**Body keys, in every product:**

| Key | Value |
|---|---|
| `event` | `"message"` |
| `session_id`, and `sessionId` with the same value | the session id |
| `action` | `"sendMessage"` |
| `chatInput` | equal to `message.text` |
| `assistant` | `{id, name}` |
| `user` | the identity (below) |
| `channel` | `{id, type}`, plus an optional product block |
| `surface` | `{type, …}` |
| `system_prompt` | a string |
| `message` | `{id, text, attachments}` |
| `history` | `[{role, text, attachments}]` |
| `events` | `[{time, kind, text}]`: always present, `[]` when there are none |

**Allowed extra keys.** Discuss AI and Live Chat may also send the legacy `contact`. Live Chat may also send `channel.livechat`.

**Session id.** `<prefix>-<first 8 hex of database.uuid>-<discuss.channel id>`.

**Attachments (ATT).**
- The keys are `{id, name, mimetype, size, url, expires_at}`.
- The url is a signed link on the product's own route, valid 1 hour.
- `expires_at` is ISO 8601 UTC.

**Identity.**
- The keys are `user_id`, `partner_id`, `name`, `email`, `lang`, `tz`, `is_portal`, `company_id`.
- Live Chat adds `guest_id`, `is_guest` and `visitor_key`.
- Only n8n receives the email.

**Ping.**
- The body is `{"event": "ping", "connection": {id, name}, "action": "loadPreviousSession", "sessionId": "<prefix>-ping-…"}`.
- Started from an assistant, it adds `"assistant": {id, name}`. Discuss AI and Live Chat may also send the legacy `contact`.
- It is signed like a message. Any HTTP 200 is a success.

**Answers.**
- Accepted shapes:
  - `{"reply": object or "text"}`;
  - `{"replies": [...]}`, of which at most 10 are used;
  - `{"skip": true}`;
  - `{"output": "text"}`;
  - an n8n item list, of which the first item is used.
- `"handoff": true` (the JSON boolean) cancels `skip`.
- `handoff_reason` keeps at most 300 characters, with white space collapsed.
- `<think>` blocks and special tokens are removed.
- Reply objects may carry attachments. Products that post reply files attach them only from the download domains. Live Chat posts text only.
- Any other shape is `invalid_response`. So is an answer with no text and no file, and without handoff or skip.

### 3.7 LiteLLM wire format

**Request.** `POST {endpoint_url}/chat/completions` with `Authorization: Bearer <key>`. The body has:
- `model`.
- `messages`:
  - one system message first, when it is not empty;
  - then turns that start and end with role `user`.
- `user`: the session id.
- `metadata`:
  - string keys and string values;
  - at most 16 keys, each value at most 512 characters;
  - the keys, in this order: `session_id`, then `surface_<key>`, then `odoo_<identity key>`;
  - never a key with the email. Live Chat also keeps `livechat_channel`.
- When streaming: `stream: true`, `stream_options: {"include_usage": true}` and the header `Accept: text/event-stream`.
- Never `tools`, `tool_choice`, `temperature` or `max_tokens`.

**Files.**
- **Images** go as data URIs: at most 4, from the last 3 user messages, each at most 5 MB.
- **Documents in `file` mode** go as `{"type": "file", "file": {"filename", "file_data"}}`: at most 5, from the last 5 user messages, each at most 4 MB. A larger document is a link.
- **Budget:** images and documents share 7 MiB of data-URI characters, about 5.25 MiB of file bytes.
- **Anything else** becomes the line `[附件] name（mimetype，size）：url`. A `[附件說明]` note is added when a document was left out.

**Answer.**
- The text is `choices[0].message.content`: a string, or its `text` and `output_text` parts. When streaming, it is the SSE deltas until `[DONE]` or a `finish_reason`.
- `<think>` blocks and special tokens are stripped.
- An empty answer is `invalid_response`. So is a stream that ends with neither `[DONE]` nor a `finish_reason`.
- Usage: prompt, completion and total tokens.
  - The `input_tokens` and `output_tokens` aliases are accepted.
  - The total is their sum when it is missing.

**System message order:**
1. the assistant's prompt;
2. the surface text: Project AI's project text and task summary, or Live Chat's notes and handoff line;
3. Discuss AI and Project AI only: an identity block (below);
4. an events block, only when there are events.

**The identity block.**
- It never contains the email.
- The person's name is one line. This rule is the minimum:
  - line breaks and runs of white space are collapsed;
  - `[` and `]` become `(` and `)`;
  - at most 80 characters are kept.
- Everybody may choose their own name, so the name can neither leave its line nor look like a history prefix.
- A product may clean more before this rule. Project AI first applies its consultant's data-text cleaning.
- Live Chat has no identity block.

### 3.8 What people see

- **Failure.** The person gets the assistant's error message after the retries fail, or when a job is too old.
  - The default is 「抱歉，我現在無法回覆，請稍後再試。」 in their language.
  - Live Chat may keep its own default text.
  - This counts as a failed reply.
- **Blocked.** One fixed notice per blocked period, posted as a notification message. Each product writes its own wording.
- **Refused.** 「您沒有使用這個 AI 助理的權限。」, where a product refuses a person.
- **Staff alerts** are product-specific:
  - Discuss AI only logs;
  - Project AI schedules a To-Do;
  - Live Chat sends an OdooBot message and a toast.

### 3.9 Logs, usage, secrets

**Call log.** One row per answer, and one per push.
- `status`: `ok`, `http_error`, `timeout`, `invalid_response` or `error`.
- Also: HTTP status, duration, error, tokens, assistant, connection, conversation, job, surface type and scope.
- `error` is at most 300 characters: a fixed text or the exception type name. It is never the exception text or the upstream body.
- Content is stored only while `log_payload` is on.
- Call logs are for administrators only. A daily cron deletes call logs and finished jobs older than 30 days.

**Usage.** One permanent row per assistant, connection, surface, scope (plus tab, where a product has tabs) and UTC month.
- Columns: `replies_ok`, `replies_failed`, and prompt, completion and total tokens.
- All columns are stored, and one SQL upsert writes them.
- n8n reports no tokens, so 0 is recorded.
- A push is counted after its commit.
- Usage is a reference for invoicing by hand, never a limit.

**Secrets.**
- Keys live only in their SQL column.
- They never appear in a field, a server log, a call log, a stored error, a notification or a payload log.
- Headers are never logged.
- Server log lines carry ids, the uid and the HTTP status. For an unexpected exception they also carry its type name and its code frames, never its text.

### 3.10 neutralize.sql

Each product ships `data/neutralize.sql`. It is not listed in the manifest: Odoo runs it for every installed module. Every top-level statement is a `DO $$ … $$` block guarded with `to_regclass` or an `information_schema` check. A database on an older version, or one whose install was interrupted, must still neutralize.

- **Connections:**
  - `endpoint_url = NULL`;
  - `api_key_secret = NULL`;
  - `active = FALSE`;
  - `customer_safe = (backend_type = 'openai')`.
- **Assistants:** `push_key_hash` and `push_key_hint` are set to NULL.
- **Legacy data:** every old table or column that still holds a URL or a key is cleared.
- **The on/off switches of the surfaces are kept**, for example Live Chat's `ai_enabled` and Discuss AI's direct chats. Inactive connections without a URL and a key already stop every call.

### 3.11 Glossary (zh_TW; source strings are English)

Where Odoo 18 has its own term, that term is used: 配置 (Configuration), 討論 (Discuss), 線上客服 (Live Chat), 客服人員 (Operator), 歡迎訊息 (Welcome Message).

| English source | zh_TW |
|---|---|
| AI Connection(s) / AI Assistant(s) | AI 連線 / AI 助理 |
| Backend / LiteLLM (OpenAI compatible) / n8n webhook | 後端 / LiteLLM（OpenAI 相容） / n8n webhook |
| Endpoint URL | 後端網址 (後端主機 in messages) |
| API Key / Signing Secret / API Key Configured / Remove API Key | API 金鑰 / 簽章金鑰 / 已設定 API 金鑰 / 移除 API 金鑰 |
| Model / Timeout (seconds) / Stream Replies / Retries | 模型 / 逾時（秒） / 串流回覆 / 重試次數 |
| Documents Users Send / Send the files to the model / Download links only | 使用者傳送的文件 / 把檔案送給模型 / 只給下載連結 |
| Download Domains / Max File Size (MB) / Log Request Content | 下載網域白名單 / 檔案大小上限（MB） / 記錄請求內容 |
| Safe for Customers / Available in Live Chat | 可給客戶使用 / 可用在 Live Chat |
| Readiness / Ready / Blocked / `<strong>Not ready:</strong>` / Readiness Details / Ready to use. | 上線檢查 / 可以使用 / 未就緒 / `<strong>未就緒：</strong>` / 檢查結果 / 可以使用。 |
| Test Connection / Connection OK (HTTP %s). | 測試連線 / 連線正常（HTTP %s）。 |
| System Prompt / History Messages / Welcome Message / Error Message | 系統提示詞 / 歷史訊息數 / 歡迎訊息 / 錯誤訊息 |
| Sorry, I cannot answer right now. Please try again later. | 抱歉，我現在無法回覆，請稍後再試。 |
| You do not have permission to use this AI assistant. | 您沒有使用這個 AI 助理的權限。 |
| Bot Partner / Reply Processing / Push Key / Archived | 機器人聯絡人 / 回覆處理方式 / 推送金鑰 / 已封存 |
| Reply Jobs / Pending / Running / Done / Failed / Skipped | 回覆工作 / 等待中 / 處理中 / 完成 / 失敗 / 已略過 |
| Runs / Run After / Trigger Message / Reply Message | 執行次數 / 最早執行時間 / 觸發訊息 / 回覆訊息 |
| Call Logs / AI Usage / Prompt Tokens / Completion Tokens / Total Tokens | 呼叫紀錄 / AI 用量 / 提示 Token 數 / 回覆 Token 數 / 總 Token 數 |

**Banned variants.** Every msgstr is checked against these: 端點, 存取金鑰, Bot 聯絡人, 招呼語, 歡迎詞, 尚未就緒, 最大歷史訊息數, 模型（選填）, Hermes. The msgid `n8n Webhook` is banned too.

**Live Chat's channel states.** A channel's AI state uses the readiness words 可以使用 and 未就緒. The word for a channel whose AI is switched off stays product wording.

### 3.12 README (light)

README.md and README_zh-TW.md must have these sections:
- **Three separate AI products:** the same table in every repository.
- **Alignment:** `docs/CONTRACT.md` and the contract version this release implements.
- **Upgrading:** what can stop answering after the upgrade, and what to check first.
- **Privacy and security.**

The other sections stay as each repository has them. Protocol details live in `docs/PROTOCOL.md`.

### 3.13 Upgrade rules (all products)

- **Bump the version** for any model, view, data or migration change. The platform upgrade gate upgrades a module only when its version changes.
- **A removed field keeps its column for one version.** Set its `ir.model.fields` xmlid to noupdate: Odoo 18 `-u` drops the columns of removed fields.
- **The pre-migrate deletes every view that uses a removed field.**
- **noupdate data** changes only through migration scripts.
- **Never delete records with a search that can match another product's records.** Restrict the search to your own model, or delete by xmlid.
- **Changed translations are reloaded** in the post-migrate. Administrators' own translations of that module are overwritten; the README says so.
- **A change that can stop an assistant answering** ships with all four of these:
  1. a post-migrate log of the affected ids;
  2. a README "before you upgrade" list;
  3. a read-only inventory query that the owner runs;
  4. a test.

  This covers the readiness gate, the model rule and the customer-safe gate.
- **Uninstall drops the module's tables**, connections and keys included. The README says so.
- **Rehearse.**
  - Run `-u all` on an approved copy of a production database where the module is installed.
  - Test the rollback to the version the site actually runs.

### 3.14 Text from records

- Customers and visitors can write some record text: their own name, a task's title or description in project sharing, a guest name, or the service data of a PaaS bridge.
- Such text reaches the model only inside a labelled block, after the facts the system sets, and never as an instruction. The kind of account comes before the name.
- A new source of such text needs a reviewer's check (section 4, item 9).

## 4. Keeping the products aligned

- **One contract and one fixture set per version.**
  - Every repository carries this file and the package of the version it implements.
  - Its `SHA256SUMS` shows at a glance whether the copies match.
- **A contract change is a new version.**
  - It gets a new package folder (v1.1, v2, …).
  - The other repositories get a tracking item and adopt it in their next PR. There is no lockstep: each repository has one open PR at a time.
- **Review checklist.** It goes into every PR body that touches an AI product, and into Live Chat's PR template.
  1. Does the change touch a contract area? These are: connection or assistant fields, roles, the queue or runner, the wire format, errors, logs and usage, neutralize, or the glossary. If yes, this contract gets a new version and the other two repositories get a tracking item.
  2. The package equals the one of the contract version the README states (`SHA256SUMS`).
  3. `test_contract` is green in the sandbox on the production core 18.0.20250807. For Live Chat, the GitHub CI must also be green.
  4. Every new string has an English source and its zh_TW from 3.11, with no banned variant and no new word for an existing concept.
  5. No key, header, upstream body, exception text or secret URL appears in logs, call logs, job errors or notifications.
  6. neutralize.sql covers every new URL, key or push-key column, with a guard.
  7. 3.13 is followed: removed fields, noupdate data, uninstall effects, and views that use removed fields.
  8. The README (3.12) and `docs/PROTOCOL.md` are updated.
  9. New record text sent to the model: who can write it? Text that customers or visitors write is labelled as data (3.14).
  10. A change that can stop an assistant answering ships with its log, inventory query, README note and test (3.13).
  11. The message trigger re-raises PostgreSQL concurrency errors.

## 5. Contract tests

**Where the tests live.**
- The package's `harness.py` holds the twelve checks.
- Each product's own `tests/test_contract.py` provides the glue:
  - the class attributes and adapter hooks that the harness docstring describes;
  - one wrapper per check, named `test_contract_01_answers` … `test_contract_12_identity_block`.
- Odoo 18 collects only the test methods a class defines itself, which is why the wrappers are needed. Check 7 fails when one is missing.
- The class is tagged `post_install`, `-at_install`, `woow_ai_contract`, and imported in `tests/__init__.py`.

**The twelve checks:**
1. **Answers.** Every case of `answers.json` (n8n, LiteLLM, and SSE where the product streams) goes through the product's own backend call with `requests.post` mocked. The normalized result equals `expect`, or `expect_text_only` in text-only products.
2. **Requests.** One canned conversation ("你是客服", "請問營業時間？") runs as a whole turn:
   - with n8n and a signing secret;
   - with LiteLLM, streamed where the product streams, then not streamed.

   The captured POST follows 3.6 and 3.7. Test Connection sends the ping, or `GET /models`.
3. **Vectors.** The signature vector and the session-id vector.
4. **Render.**
   - Links that are allowed or refused, with `\` and userinfo refused.
   - 1000 nested `>` and 200 nested list levels render without an exception and without losing the text.
   - The identity names are printed cleaned (products with an identity block).
5. **Glossary.**
   - Every glossary msgid in the module's zh_TW.po has exactly the glossary msgstr.
   - No msgstr has a banned variant.
   - `fields_get` in zh_TW gives the glossary labels by field and selection key.
6. **Neutralize.**
   - Every statement of `data/neutralize.sql` is a guarded `DO` block.
   - Run over seeded records, it leaves the state of 3.10.
7. **Sums.** `SHA256SUMS` matches the files. The product glue defines the twelve wrappers.
8. **Secrets.** A LiteLLM connection with a key goes through four failures: HTTP 401, a read timeout, an unexpected exception and a header error. The key and the upstream body appear nowhere: not in the log lines (Odoo's own `--log-sql` query log excepted), the call logs, the job errors or the messages.
9. **customer_safe.**
   - LiteLLM is True on create, write, copy and in the form, and the constraint refuses False.
   - n8n is False on create, and is cleared when the URL or the type changes.
10. **Retries and deadline.** The rules of 3.3, with `time.sleep` mocked:
    - 429 and 503 are retried after 3 s and 10 s;
    - a request that was not sent is retried;
    - a read timeout, a 401, a reset after sending, 502, 504 and a redirect are not retried;
    - no retry is started past the deadline.
11. **Gate.**
    - A blocked connection gives no job and no HTTP call.
    - It gives one notification per blocked period.
    - The AI answers again once the connection is ready.
12. **Identity block.**
    - The email never reaches LiteLLM.
    - The name is cleaned (Discuss AI, Project AI). Live Chat has no identity block.

## 6. Package files and digests

**Copies.** The source of truth is `work/ai-backend-align-20261123/contract/v1/`. A repository copies it byte for byte:
- `docs/CONTRACT.md` (this file) to `docs/CONTRACT.md`;
- `MODULE/tests/contract/` to `<module>/tests/contract/`.

**SHA256SUMS** lists the files of `tests/contract/` and `../../../docs/CONTRACT.md`. The `READY` file of the source folder holds the SHA-256 of `SHA256SUMS`.

**Fixture digest** (SHA-256 of the files of `tests/contract/`):

| File | SHA-256 |
|---|---|
| `__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `answers.json` | `f759b9dd9b43d7a5f4ec4c879a7d6cc9f6a390deb0f0a79e5d098b77aa462fef` |
| `glossary.json` | `8937f142e3520cea42ad481eda30552aa3bdb9da99295a1b556c170ce4ccdda2` |
| `harness.py` | `813e39295a4557733c2a40d9155d22f32ea518a46695c7e809441161d461f0af` |
| `render.json` | `138807403863bea87b76159e4af48870611d3d20de2f2fb2a559b8e0759a21b5` |
| `requests.json` | `2dbf349572a3ee36cf7917be60d6a922342468134913c39b219b5dbebcb3f4ea` |
