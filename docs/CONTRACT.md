# AI products contract, version 1.3

- **Version:** v1.3, 2026-11-28. It replaces v1.2, which replaced v1.1 and v1. The rules of v1 stay; v1.1 states
  rules that v1's fixtures only implied, adds the speaker label (3.14) and the run-time gate (3.5), and its checks
  prove more; v1.2 adds the rule that rows created for a post get explicit values only (3.5, check 14); v1.3 adds
  the current time (3.6, 3.7), removes a speaker label a model copies into its answer (3.14), lets a turn go on
  when the model cannot read a file (3.7, check 15), and extends the explicit-values rule to `ir.default` rows
  (3.5, check 14).
- **Products:**
  - Discuss AI (`woow_ai_contact`);
  - Project AI (`woow_ai_task`);
  - Live Chat AI (`im_livechat_ai`).
- **Status:** normative. "Must" and "never" are requirements; "may" is a permitted choice.

The three products are separate. Each keeps its own runtime core and ships on its own, and none of them uses another one's code. This contract is what makes them configured and run the same way. Every repository carries a byte-identical copy of this file as `docs/CONTRACT.md`, and the contract package as `<module>/tests/contract/` (section 6). A product may lag a version: its README names the version it implements.

## 1. Decisions this contract implements

- **Three cores, three downloads.** Live Chat AI is upgraded in place. It is not merged into another core and not retired.
- **n8n or LiteLLM only.** The stored backend key `openai` stays, labelled 「LiteLLM（OpenAI 相容）」.
- **A LiteLLM connection is always 「可給客戶使用」**, the same in the three products.
- **Two layers.** 「AI 連線」 (connection) is for system administrators only. 「AI 助理」 (assistant) uses a connection.
- **Live Chat assistants.**
  - A system administrator ticks 「可用在 Live Chat」 on an assistant. Live Chat managers can pick only those assistants.
  - The connection is not forced to be safe for customers; the AI tab warns instead.
- **Every message gets an answer.**
  - Replies are always queued on the server.
  - Every connection has a test and a readiness check.
- **Usage, not billing.**
  - Usage counts replies, plus tokens when the backend reports them.
  - There is no billing logic and no per-use charging. Limits live in the LiteLLM keys.
- **Discuss AI and portal customers.** A portal customer gets no answer through a connection that is not 「可給客戶使用」. The upgrade ticks the n8n connections already shared with customers first.

## 2. Terms

- **Connection:** a record of the 「AI 連線」 model: the backend type, URL, key and call settings.
- **Assistant:** a record of the 「AI 助理」 model: the persona that answers, with its connection.
- **Conversation:** the `discuss.channel` an answer is given in.
- **Answer:** the backend requests of one turn: a request with its retries (3.3), plus any fallback request (3.7),
  and what the first successful one returns.
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
| Speaker label before a person's text (3.14) | no | yes: `[<name>] ` in the internal chat, `[客戶 <name>] `, `[負責同事 <name>] ` and `[其他 <name>] ` in the customer chat | no |
| Conversation events in the n8n `events` | none | none | yes |
| Job fields: state, runs, start, error | `state`, `run_count`, `started_at`, `error` | `state`, `run_count`, `started_at`, `error` | `state`, `attempts`, `started_at`, `error` |

**Test values.** `requests.json` ("products") holds the values of this table that the tests use, per module:
`surface_type`, `session_prefix`, `text_only`, `streams`, `identity_block`, `link_policy`, `speaker_label` and
`events`; check 7 compares the product's test class with them. It also names the job fields check 14 forges.

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
| `model` | LiteLLM: needed to be ready. A connection without one is blocked and Test Connection refuses it, but it is not a required field: the form saves it. n8n: not used. |
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
- LiteLLM is always True. Every path enforces it:
  - `create`, `copy` and a `write` that sets `backend_type` to LiteLLM set it to True, even when the same values
    say False;
  - an explicit `write` of `customer_safe = False` on a LiteLLM connection raises a ValidationError (the
    constraint);
  - the form's onchange shows True as soon as the type is LiteLLM.
- n8n is False until a system administrator ticks it. It is cleared when the backend type or the endpoint URL changes.

**`readiness`.**
- It is `ok` or `blocked`, and it is searchable.
- A connection is blocked when it is archived, has no URL, or (LiteLLM) has no key or no model.
- `readiness_reason` is made of fixed sentences, never a URL or a key.

**`api_key`.**
- It is a compute/inverse field over the SQL-only column `api_key_secret`. Only administrators may set it.
- LiteLLM: needed to be ready (like `model`), sent as `Authorization: Bearer`.
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
- Redirects are never followed: a 3xx answer is `http_error`.
- The same rules apply to both backends.

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
   - **Again when the job runs, and before it posts the answer.** An administrator may change the connection,
     the assistant or the sharing while a job waits. A job that is blocked then makes no call (or posts no
     answer) and is `skipped`; the person gets the notice, once per blocked period. A job whose person may no
     longer use the assistant is `skipped` without a notice.
3. **The job.**
   - States: `pending`, `running`, `done`, `failed`, `skipped`.
   - It is created in the poster's transaction, with a 1.5 s debounce, and runs at most 3 times.
   - Lost jobs are re-queued: pending and 30 s late, or running for 60 s with a free lock.
   - A job older than 30 minutes is given up, unless it no longer needs an answer.
     - The person then gets the error message.
     - Live Chat alerts its operators instead.
   - Job kinds are product-specific.
   - **Explicit values only.** The job is created in the poster's request, and that request carries the
     client's context: Odoo's `/mail/message/post` applies it, and a guest, a portal user or an employee can
     send `default_<field>` keys in it. `create` fills every field it is not given from such keys, also under
     `sudo()`. So every row a product creates with `sudo()` while it handles a post or a public or portal
     route (the job, and call logs, files, chats) is created with those keys dropped
     (`with_context(clean_context(self.env.context))`; `with_context(**values)` only merges). Odoo 18's
     `sudo()` drops them only when it switches into superuser mode; code already in that mode, or a context
     applied after `sudo()`, keeps them. A forged running state with a future start would pin the
     conversation, and a forged run count would make every message fail without a call.
   - **`ir.default` rows fill a field the same way.** `create` also takes the value of a field it is not
     given from `ir.default` (a global row, or the posting user's own: an internal user may write rows for
     themselves). Cleaning the context is not enough: the job row is created with every field of the job
     fields of 3.1, and every other field the runner reads, passed explicitly.
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
- The body is compact UTF-8 JSON (`ensure_ascii=False`, `separators=(',', ':')`). Those exact bytes are signed and sent
  as they are: `data=<bytes>`, never `json=` and never a `str`.
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
| `message` | `{id, text, attachments}`: the message being answered; `text` as the person typed it, after the speaker label in a product that labels (3.14) |
| `history` | `[{role, text, attachments}]`: the earlier messages, oldest first; `role` is `user` or `assistant`; an assistant's text as it was posted |
| `events` | `[{time, kind, text}]`: always present, `[]` when there are none |
| `now` | `{utc, local, tz, weekday}`: the current time when the request is built (3.7, "The current time") |

**Allowed extra keys.** Discuss AI and Live Chat may also send the legacy `contact`. Live Chat may also send `channel.livechat`.

**Session id.** `<prefix>-<first 8 hex of database.uuid>-<discuss.channel id>`.

**Attachments (ATT).**
- The keys are `{id, name, mimetype, size, url, expires_at}`; `size` is the file's size in bytes.
- The url is an absolute signed link on the product's own route, valid 1 hour.
- `expires_at` and `events[].time` are ISO 8601 UTC in the form `YYYY-MM-DDTHH:MM:SSZ`.

**Identity.**
- The keys are `user_id`, `partner_id`, `name`, `email`, `lang`, `tz`, `is_portal`, `company_id`.
- `name` is the person's name as stored; a name with an `@` counts as no name and is `""` (3.14).
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
- `{"skip": true}` without a handoff wins over a reply: nothing is posted.
- `"handoff": true` (the JSON boolean) cancels `skip`.
- `handoff_reason` counts only with `"handoff": true`. White space is collapsed first, then at most 300 characters are kept.
- `<think>` blocks and special tokens are removed.
- A speaker label the model copied to the start of a reply text is removed (3.14, "Labels in answers").
- **Reply files.** A reply object may carry `attachments`: `{url, name}` or `{data_base64, name}`.
  - A file fetched by URL comes only from the download domains (3.3).
  - `data_base64` items, and `data:image` URIs in the text, are decoded without a download, with the same size
    limit.
  - A reply with an empty text and a file is valid where replies post files: its text is `""`.
  - Text-only products (Live Chat) post the text and drop the files; for them a reply with only a file has no
    text.
- Any other shape is `invalid_response`. So is an answer with no text and no file, and without handoff or skip.

### 3.7 LiteLLM wire format

**Request.** `POST {endpoint_url}/chat/completions` with `Authorization: Bearer <key>`. The body is UTF-8 JSON
bytes: `data=<bytes>`, or `json=<object>`, which requests sends as bytes; never a `str`. It has:
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
- **Fallback when the model cannot read a file.** A model, or the route LiteLLM picks, may refuse a file type,
  and the files of the last messages are sent again with every turn: without a fallback, one such file would
  make every turn fail until it leaves the window.
  - When a request that carries file parts (inline images, or documents in `file` mode) gets HTTP 400, 413,
    415, 422 or 500, the product sends the same turn again at once: first without the file parts of earlier
    messages; then, when the answer is again one of these statuses and file parts are left, without any file
    part.
  - A file left out becomes its `[附件]` line, followed by the note
    `[附件說明] <name>：模型無法讀取這個檔案，只附下載連結。`
  - These requests are not retries in the sense of 3.3: there is no wait, and `max_retries` does not count them.
    The deadline of the answer bounds them all: a fallback request is not started with less than one second left.
    Each request writes its own call log row. The answer is the
    first one that succeeds; when none does, the turn fails as before.
  - Only these statuses, and only a request with file parts: a request without files, and any other status,
    are handled exactly as before.
  - Before each fallback request the run-time gate of 3.5 is checked again, as before a retry: when it blocks, no
    request is sent. Each fallback request is built again (its current time too), with the same choice of files
    minus the ones it leaves out.
  - **The conversation remembers.** When a later request of the same turn succeeds, the files that request left
    out are never sent as a file part again in that conversation: where such a file would be a file part, later
    requests carry its line and the same note instead, so a later turn does not fail first. When no request of the turn succeeds (the backend may be down for
    every request), nothing is remembered. A job that runs again reads what the conversation remembers. The memory
    lasts as long as the conversation: changing the connection or the model does not clear it, and each product's
    README says how an administrator clears it.
**Answer.**
- The text is `choices[0].message.content`: a string, or its `text` and `output_text` parts joined with a line break. When streaming, it is the SSE deltas until `[DONE]` or a `finish_reason`.
- An error object in a stream is `invalid_response`.
- `<think>` blocks and special tokens are stripped.
- A speaker label the model copied to the start of the answer is removed (3.14, "Labels in answers"), also when
  the answer was streamed.
- An empty answer is `invalid_response`. So is a stream that ends with neither `[DONE]` nor a `finish_reason`.
- Usage: prompt, completion and total tokens.
  - The `input_tokens` and `output_tokens` aliases are accepted.
  - The total is their sum when it is missing.
  - A later usage chunk with zeros keeps the earlier real counts.

**System message order:**
1. the assistant's prompt;
2. the surface text: Project AI's project text and task summary, or Live Chat's event notes (operator, resume)
   and handoff line;
3. Discuss AI and Project AI only: an identity block (below);
4. the current time, always, as exactly one line (3.7, "The current time"):
   `Current time: YYYY-MM-DD HH:MM (<weekday>), time zone <tz> (UTC±HH:MM).`
   For example `Current time: 2026-11-28 10:20 (Saturday), time zone Asia/Taipei (UTC+08:00).`;
5. Discuss AI and Project AI: an events block, only when there are events (Live Chat's events are its notes, item 2).

**The identity block.**
- It never contains the email, not even as a name.
- Its lines are `- <label>: <value>`. The kind of account comes first (`- account: …`), then the ids, the language
  and the time zone; the name comes last.
- The person's name is one line. This rule is the minimum:
  - line breaks and runs of white space are collapsed;
  - `[` and `]` become `(` and `)`;
  - at most 80 characters are kept;
  - a name with an `@` counts as no name: the line is left out.
- Everybody may choose their own name, so the name can neither leave its line nor look like a history prefix.
- A product may clean more before this rule. Project AI first applies its consultant's data-text cleaning.
- Live Chat has no identity block.

**The current time.** It is computed once per request, when the request is built:
- `utc`: ISO 8601 UTC, `YYYY-MM-DDTHH:MM:SSZ`;
- `local`: the same instant in the time zone below, `YYYY-MM-DDTHH:MM:SS±HH:MM`;
- `tz`: the IANA name of that time zone;
- `weekday`: the English name of the day of `local` (`Monday` … `Sunday`).

The time zone, the first one set of:
1. the company's: the time zone of the working schedule of the current company (`env.company.resource_calendar_id.tz`,
   where the Resource module is installed; it is on the working schedule form), else the `tz` of the company's
   partner (no standard form shows it);
2. the person's own: the identity's `tz`; in Live Chat, the visitor's: a guest's `timezone`, else the user's `tz`;
3. `UTC`.

The current company is the one the job runs with (`env.company`; the database's main company for a job run by the
superuser). A name that is not a known IANA time zone counts as none. n8n gets the four values as `now` (3.6);
LiteLLM gets one line in the system message (the order above). Without it a model guesses the date, and may tell a
customer that the shop is open on a day it is closed.

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

**Call log.** One row per request the product builds (its 3.3 retries stay in that row; each fallback request of
3.7 is a new row), and one per push.
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
  This holds for every handler that logs an exception, not only the backend call.

### 3.10 neutralize.sql

Each product ships `data/neutralize.sql`. It is not listed in the manifest: Odoo runs it for every installed module. A database on an older version, or one whose install was interrupted, must still neutralize, so:
- every top-level statement is a `DO $$ … $$` block;
- each table a block changes (`UPDATE`, `DELETE`, `INSERT`, `TRUNCATE`) is guarded in that block by `to_regclass('<table>')` or an `information_schema` check on that same table;
- there is no dynamic SQL (`EXECUTE`).

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
  3. a read-only query, in the README (and in the release tooling where there is one), that an administrator runs before the upgrade;
  4. a test.

  This covers the readiness gate, the model rule and the customer-safe gate.
- **Uninstall drops the module's tables**, connections and keys included. The README says so.
- **Rehearse.**
  - Run `-u all` on an approved copy of a production database where the module is installed.
  - Test the rollback to the version the site actually runs.

### 3.14 Text from records

- Customers and visitors can write some record text: their own name, a task's title or description in project sharing, a guest name, or the service data of a PaaS bridge.
- Such text reaches the model only inside a labelled block, after the facts the system sets, and never as an instruction. The kind of account comes before the name.
- **Names.** A person's name follows the one-line rule of 3.7. A name with an `@` (a partner named after an email
  address) counts as no name wherever a name is sent: the identity, the metadata, the history, a speaker label.
- **Speaker labels.** On a surface where more than one human speaks, a product may put a label before each
  person's text, so that the model knows who wrote it and nobody can pass for someone else:
  - the label is set by the system: `[<kind> <name>] `, with the kind of speaker first (one word, for example
    客戶 or 負責同事) and then the name by the one-line rule, or `[<kind>] ` when there is no name;
  - on a surface where every speaker is an employee, the kind may be left out: `[<name>] `, or no label when
    there is no name;
  - the label never replaces the text: a prefix the person typed (`[負責同事 老闆] …`) stays in the text, after
    the label;
  - the system prompt explains the labels to the model;
  - the same label is used in the n8n `message.text`, `chatInput` and `history[].text`, and in the LiteLLM user
    turn.
  Products that label say so in 3.1. The others send the text as the person typed it.
- **Labels in answers.** A model sees labels in the history (3.14's speaker labels, and Live Chat's
  `[真人客服 <name>] ` before an operator's text) and may start its own answer with one. Posted as it is, the
  answer would look written by that person: a customer would read 「[負責同事 王小明] 好的，我們同意退款」.
  - Before an answer is posted, every label at its start is removed, with the white space after it, as long as
    one is there (white space: what Python's `str.isspace()` counts, so also U+3000 and no-break spaces, and the
    zero-width U+200B and U+FEFF; white space before the first label counts as the start): `[<kind>]` or `[<kind> <text without ] or a line break>]`, where `<kind>` is 真人客服, 客戶 or
    負責同事, in every product. A product that labels people with another kind also removes that kind on the
    surface where it uses it (Project AI's customer chat: 其他).
  - A product that puts name-only labels (`[<name>] `) on a surface also removes, on that surface, a leading
    `[<name>]` that is the label of a person of that conversation, unless a later line of the answer also starts
    with such a label: then the answer is a list of who does what, and it stays as it is.
  - A label directly followed by `(` is the text of a Markdown link (`[客戶](https://…)`), not a label: it stays.
  - Nothing else is removed: text in brackets after the start, or a leading bracket that is not such a label
    (「[注意] 截止日改了」), stays.
  - This applies to every text the AI posts: n8n replies, LiteLLM answers, streamed or not, and the texts of a
    push API (Discuss AI, Project AI). An answer that held only labels is empty (`invalid_response`); a push text
    that held only labels is refused like an empty one.
- A new source of such text needs a reviewer's check (section 4, item 9).

## 4. Keeping the products aligned

- **One contract and one fixture set per version.**
  - Every repository carries this file and the package of the version it implements.
  - Its `SHA256SUMS` shows at a glance whether the copies match.
- **A contract change is a new version.**
  - It gets a new package folder (v1.1, v2, …) in the contract folder of the release workspace. That folder is the
    source until a repository has merged the version.
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
- The package's `harness.py` holds the fifteen checks.
- Each product's own `tests/test_contract.py` provides the glue:
  - the class attributes and adapter hooks that the harness docstring describes;
  - one wrapper per check, named `test_contract_01_answers` … `test_contract_15_file_fallback`.
- Odoo 18 collects only the test methods a class defines itself, which is why the wrappers are needed. Check 7 fails when one is missing, skipped, or does not call its check.
- The class is tagged `post_install`, `-at_install`, `woow_ai_contract`, and imported in `tests/__init__.py`.
- Run them at Odoo's default or info log level. Check 8 turns the `odoo` loggers to DEBUG while it runs.

**The fifteen checks:**
1. **Answers.** Every case of `answers.json` (n8n, LiteLLM, and SSE where the product streams) goes through the product's own backend call with `requests.post` mocked. The normalized result equals `expect`, or `expect_text_only` in text-only products.
2. **Requests.** One canned conversation ("你是客服", "請問營業時間？") runs as a whole turn:
   - with n8n, signed and unsigned;
   - with LiteLLM, streamed where the product streams, then not streamed;
   - after an earlier turn (the person's message with an image, the assistant's answer, and an event where the
     product records events): the n8n history, ATT and events, and the LiteLLM turns and the inline image;
   - with the speaker-label cases: a name that looks like a kind of speaker, a name with brackets, a name with an
     `@`, and a typed fake label.

   The captured POST follows 3.6, 3.7 and 3.14, and its body bytes are checked as sent. Every request carries the
   current time: the n8n `now` has its four keys, its `utc` is within five minutes of the test's clock, and its
   `local`, offset and `weekday` agree with `tz`; the LiteLLM system message has exactly one time line, after the
   identity block where there is one, that agrees the same way. Test Connection sends the
   ping, or `GET /models`.
3. **Vectors.** The signature vector and the session-id vector.
4. **Render.**
   - Links that are allowed or refused, with `\` and userinfo refused.
   - 1000 nested `>` and 200 nested list levels render without an exception and without losing the text.
   - The identity names are printed cleaned, after the kind of account, and names with an `@` are left out
     (products with an identity block).
5. **Glossary.**
   - Every glossary msgid in the module's zh_TW.po has exactly the glossary msgstr.
   - No msgstr has a banned variant.
   - `fields_get` in zh_TW gives the glossary labels by field and selection key.
6. **Neutralize.**
   - Every statement of `data/neutralize.sql` is a `DO` block, every table it changes has its own guard, and there
     is no dynamic SQL.
   - Run over seeded records, it leaves the state of 3.10.
7. **Sums.** `SHA256SUMS` matches the files, and `READY` holds its SHA-256. In a repository checkout
   `docs/CONTRACT.md` is there. The product glue defines the fifteen wrappers, none skipped, each calling its
   check, and its values equal the table of 3.1.
8. **Secrets.** Four failures (HTTP 401, a read timeout, an unexpected exception and a header error) on a LiteLLM
   connection with a key, on a streamed one where the product streams (plus an error inside the stream), and on an
   n8n connection whose signing secret and URL are secrets. The secrets and the upstream body appear nowhere: not
   in any log record at any level (Odoo's own SQL query log excepted), the call logs, the job errors or the
   messages.
9. **customer_safe.** The rules of 3.3:
   - LiteLLM is True on create, copy, a change of type and in the form, and an explicit write of False raises;
   - n8n is False on create, and is cleared when the URL or the type changes.
   The form used is the product's default form view, with `name`, `endpoint_url`, `backend_type` and
   `customer_safe` on it; it saves a LiteLLM connection without a model.
10. **Retries and deadline.** The rules of 3.3, for LiteLLM and for n8n, with `time.sleep` mocked:
    - 429 and 503 are retried after 3 s and 10 s;
    - a request that was not sent is retried;
    - a read timeout, a 401, a reset after sending, 502, 504 and a redirect are not retried;
    - no retry is started past the deadline.
11. **Gate.**
    - A blocked connection gives no job and no HTTP call.
    - It gives one notification per blocked period.
    - The AI answers again once the connection is ready.
    - A job queued while the AI could answer, then blocked before it runs, makes no call and gives the notice.
12. **Identity block.**
    - The email never reaches LiteLLM.
    - The name is cleaned, after the kind of account, and left out when it has an `@` (Discuss AI, Project AI).
      Live Chat has no identity block.
13. **Connections.**
    - A new n8n connection waits 120 s.
    - The API key never comes back from `read` or an export.
    - A user who is not a system administrator cannot read connections.
14. **Job rows.** A person posts through the product's real path, from a superuser environment, with a
    context that carries `default_<state>`, `default_<runs>`, `default_<start>` and `default_<error>` (the
    job fields of 3.1), and with global `ir.default` rows that set the same four fields: the job row is pending,
    with no run, no start and no error.
15. **File fallback.** A LiteLLM turn, not streamed, after an earlier message with an image, where the backend
    answers HTTP 400 to every request that carries a file part (3.7, "Fallback when the model cannot read a
    file"):
    - the product sends the turn again at once without the earlier image: two requests, and no wait;
    - the second request carries the image's `[附件]` line and the fallback note, and no file part;
    - the answer is posted;
    - the person then asks again in the same conversation: one request, without the image as a file part, with
      its line and the note (the conversation remembers).

**What the checks do not cover.** Each product's own suite covers these:
- the roles of assistants, jobs, call logs and usage (3.2), beyond the connection rule of check 13;
- the endpoint URL rule and the timeout ranges (3.3);
- the job lifecycle: debounce, three runs, re-queueing, the 30-minute limit, one job per conversation, the
  runner and its recovery (3.5);
- the error message a person gets after a failure, and staff alerts (3.8);
- call-log rows, the daily cleanup and the usage rows (3.9);
- Test Connection's wording (3.3);
- the numeric limits of 3.7 (metadata values, the file budget, document sizes, the stream's idle time);
- the file fallback beyond check 15: the other statuses, documents, the step without any file part, call log rows;
- the time zone choice of the current time (company, person, `UTC`);
- the name-only labels in answers (3.14), where a product uses them;
- server log lines of handlers other than the backend call (3.9);
- the README sections (3.12) and the upgrade rules (3.13).

## 6. Package files and digests

**Copies.** The source is the `contract/v1.3/` folder of the release workspace, until a repository has merged this
version. A repository copies it byte for byte:
- `docs/CONTRACT.md` (this file) to `docs/CONTRACT.md`;
- `MODULE/tests/contract/` to `<module>/tests/contract/`.

**SHA256SUMS** lists the files of `tests/contract/` and `../../../docs/CONTRACT.md`. `READY`, in the source folder and
in `tests/contract/`, holds the SHA-256 of `SHA256SUMS`; check 7 compares them.

**Fixture digest** (SHA-256 of the files of `tests/contract/`):

| File | SHA-256 |
|---|---|
| `__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `answers.json` | `050edf4f2803d39573bd52d8832ac3ebe7b114dd5c593284e9fd190b0cd9658d` |
| `glossary.json` | `0348148c7d7e1206b9e387e84e6bd323ead3b098ca7599eef53cf5482b2049be` |
| `harness.py` | `bfe0ae9ebc67312f6610ff9b45c78c323714dda407df22e0187df3ec9f0d72b4` |
| `render.json` | `08992ad0d56588c46d6594688676ed5186b93837323e88ed98a2c04e1abbdee1` |
| `requests.json` | `925f1a77bba78c77ecbe7117e21cd24d967f54622ce113de1705e336d0d56768` |
