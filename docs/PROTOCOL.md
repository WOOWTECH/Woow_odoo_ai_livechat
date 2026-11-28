# Backend protocol (Live Chat AI)

What `im_livechat_ai` 18.0.2.1.0 sends to an AI backend and what it accepts back. It implements
[contract v1.3](CONTRACT.md) (sections 3.3 to 3.9 and 3.14), the same wire format as Discuss AI and Project AI, with
the Live Chat values of section 3.1: session id prefix `woow-ai`, `surface.type` `livechat`, whole (not
streamed) LiteLLM replies, text-only replies, clickable links only to the site's own hosts, and no
identity block in the system message.

## Where the settings live

| Layer | Who | Menu | Settings |
|---|---|---|---|
| **AI connection** (`im_livechat.ai.connection`, 「AI 連線」) | system administrators | Live Chat > Configuration > AI Connections | backend (LiteLLM or n8n), endpoint URL, API key or signing secret, model, timeout, retries, how documents are sent, request content logging, safe for customers, readiness |
| **AI assistant** (`im_livechat.ai.assistant`, 「AI 助理」) | system administrators | Live Chat > Configuration > AI Assistants | name and avatar (its bot partner), its AI connection, system prompt, history messages, error message, 「可用在 Live Chat」 |
| **Live Chat channel**, tab 「AI 助理」 | Live Chat managers | Live Chat > Configuration > Channels | Answer With AI, which AI assistant (only those ticked 「可用在 Live Chat」), welcome message, handoff message, waiting notice, pause and wait minutes, optional handoff keywords |

Several assistants may share one connection, and several channels one assistant. Live Chat managers
never see a URL or a key: the channel shows the assistant's description, its state and, when its
connection is not marked 「可給客戶使用」, a warning.

## Limits and queue caps

**Rate limits live in the LiteLLM key.** Give every Live Chat assistant's connection its own LiteLLM
virtual key, restricted to its model, with requests and tokens per minute, parallel requests and a
budget. Odoo has no usage limit: the monthly usage (Live Chat > Configuration > AI Usage) is a reference
for invoicing by hand.

Odoo only has technical queue caps:

| Cap | Value |
|---|---|
| Replies at a time per conversation | one (an advisory lock per conversation); a newer visitor message supersedes the pending reply, which then answers all of them |
| Unfinished replies per Live Chat channel | 30; system parameter `im_livechat_ai.max_active_jobs` (0 turns the cap off) |
| Unfinished replies of the whole database | 60; system parameter `im_livechat_ai.max_active_jobs_total` (0 turns the cap off): an abuse bound, whatever the processes |
| Reply threads holding a connection, per Odoo process | half of what the process's connection pool (`db_maxconn`) leaves after the cron threads, at least 1; the other half stays free for web requests. A reply thread takes a slot before it opens its database cursor and waits for one holding no connection; after 5 minutes (1 minute for the recovery cron's threads) it gives up and the job stays pending for the recovery cron |
| A message a cap refuses | no reply job; the conversation gets the blocked notice once per period, the operators one alert per period and at most one per channel in 10 minutes (its own alert: it does not hide the blocked alert), the server log a warning at most every 10 minutes |
| Debounce | 1.5 s: rapid visitor messages get one reply |
| Runs of one reply job | at most 3 (a run is restarted only after a crash or a restart) |
| Age | a reply job older than 30 minutes is given up; the operators are alerted |
| Failure alerts | the visitor gets the error message every time; the operators get at most one alert per kind of failure (HTTP 429, HTTP 500, a timeout, an aged-out job ...) and channel in 10 minutes, counted from that alert (jobs that fail together in several threads claim it one after the other) |

## Common context

- **Session id**: `woow-ai-<first 8 hex of database.uuid>-<discuss.channel id>`, stable for one
  conversation (the same rule as Discuss AI: a `discuss.channel` id never repeats in one database).
- **History**: the last *History Messages* (1-200, default 50) messages of the conversation, oldest
  first. Visitor messages are `user` turns. Operator messages are `user` turns starting with
  `[真人客服 <name>] `, so the model reads them as a colleague's words. AI replies are `assistant` turns.
  Notification messages (the blocked notice, rating notices) and internal notes are never part of it.
- **Events**: facts the model must know, `{time, kind, text}`: `operator` (operators wrote in the
  conversation) and `resume` (a handoff wait ended and no operator answered).
- **System prompt**: the AI assistant's prompt. Odoo adds no persona.
- **Current time** (contract 3.6, 3.7): every request carries the time it was built at, so the model never guesses
  the date (the staging bench had a model tell a customer on a Saturday that it was Wednesday). n8n gets `now`,
  LiteLLM one line that ends the system message. The time zone is the current company's (its partner's time
  zone), else the visitor's (a guest's time zone, else the user's), else `UTC`; a name that is not a known IANA
  time zone counts as none.
- **Names** (contract 3.14): Live Chat sends a person's text as typed (no speaker label). A name with an `@` (a
  partner named after an email address) counts as no name; a name inside text the model reads (the operator's
  name in `[真人客服 <name>] `, the metadata) follows the one-line rule: one line, brackets as parentheses, at
  most 80 characters.
- **Visitor identity** (n8n only): `user` with `user_id`, `partner_id`, `guest_id`, `name`, `email`, `lang`,
  `tz`, `is_portal`, `is_guest`, `company_id` and `visitor_key` (`guest:<id>`, `partner:<id>`, or `public`
  for the website's public user). Never the IP address, visited pages or tracking data. LiteLLM never
  receives the email, and its system message has no identity block: visitors are mostly anonymous.

## Timeouts and retries

- The connection's **Timeout (seconds)** is the time for one answer, retries included: 10 to 300 s; new
  connections get 45 s (LiteLLM) or 120 s (n8n). Each call waits at most the time left for the backend (the
  wait between received bytes), after at most 10 s to connect, so a backend that keeps sending bytes slowly can
  take longer.
- A call is retried (at most the connection's **Retries**, 0 to 2, default 1) only when the request was not
  sent (connection refused, unknown host, connect timeout) or the backend answered HTTP 429 or 503. The
  waits are 3 s, then 10 s, and a retry never starts when its wait would reach the deadline.
- Never retried: a read timeout, a reset after the request was sent, any other answer (401, 502 and 504
  included). Redirects are never followed: enter the final URL.
- A long n8n workflow should use an in-cluster URL (`http://<service>.<namespace>.svc.cluster.local`), or
  answer `{"skip": true}` at once. Cloudflare cuts a request after about 100 s.

## LiteLLM backend (`openai`)

```http
POST {endpoint}/chat/completions
Authorization: Bearer <API key>
Content-Type: application/json
```

The body is compact UTF-8 JSON:

```json
{
  "model": "chatgpt-pool/gpt-6-sol",
  "messages": [
    {"role": "system", "content": "<the assistant's prompt, the event notes, the handoff line, the time line>"},
    {"role": "user", "content": "請問營業時間？"}
  ],
  "user": "woow-ai-0edfec80-42",
  "metadata": {"session_id": "woow-ai-0edfec80-42", "surface_type": "livechat",
               "surface_livechat_channel_id": "3", "odoo_guest_id": "7", "odoo_name": "訪客",
               "odoo_lang": "zh_TW", "odoo_is_portal": "false", "odoo_is_guest": "true",
               "livechat_channel": "官網客服"}
}
```

- `model` is required: a LiteLLM connection without a model is not ready. Never `tools`, `tool_choice`,
  `temperature`, `max_tokens` or `stream`: those are set on the LiteLLM side.
- `messages`: one system message first when it is not empty, then turns that start and end with a
  `user` turn (the welcome message before the first visitor message is dropped; AI replies after the last
  visitor message are moved before it).
- The system message is the assistant's prompt, the event notes, the handoff line when the channel has operators
  (the model ends its reply with a line `[[handoff]]` to hand the visitor to a human; the marker is removed before
  posting), then always the current time as one line, for example
  `Current time: 2026-11-28 10:20 (Saturday), time zone Asia/Taipei (UTC+08:00).` Live Chat has no identity block,
  and the time is not in the metadata.
- `metadata`: string keys and values, at most 16 keys of at most 512 characters: `session_id`, the surface
  as `surface_<key>`, the visitor as `odoo_<key>` (never the email), then `livechat_channel`.
- **Answer**: `choices[0].message.content`, a string or its `text` and `output_text` parts (joined with a
  line break). `<think>` blocks and special tokens such as `<|im_end|>` are removed, then every speaker label at
  the start (see [Labels in answers](#labels-in-answers)). An empty answer is `invalid_response`.
- **Usage**: `usage.prompt_tokens`, `completion_tokens` and `total_tokens` (the `input_tokens` and
  `output_tokens` aliases are accepted; the total is their sum when it is missing).

### Visitor files

Live Chat visitors stay text-only: the module does not enable file upload in the chat widget (owner
decision 41). The rules below apply to files that do reach a conversation, for example from portal users or
employees chatting as visitors. Attachments of AI, OdooBot and operator messages are never sent. Odoo
extracts no text and converts nothing.

| What | How it is sent to LiteLLM |
|---|---|
| Images (png, jpeg, gif, webp; the type is detected from the content) | an `image_url` data URI: only the 3 most recent visitor messages, at most 4 images, 5 MB each |
| Documents (PDF, docx, xlsx, xls, csv, txt, md, json) when the connection's *Documents Users Send* is 「把檔案送給模型」 | a `{"type": "file", "file": {"filename", "file_data"}}` part with the original bytes and mimetype, plus a line `[附件] <name>（<mimetype>，<size>）：檔案已附在這則訊息`: only the 5 most recent visitor messages, at most 5 documents, 4 MB each |
| Documents in 「只給下載連結」 mode, older or larger files, other types | a line `[附件] <name>（<mimetype>，<size>）：<signed url>`; a file left out because of a limit adds a `[附件說明]` note |
| Active or dangerous types (html, svg, xml, javascript, executables, zip archives with executables, archives Odoo cannot look into) | always a link, never inlined |

Images and documents share **7 MiB** of data-URI characters per request (about 5.25 MiB of file bytes).
With *Log Request Content* on, the base64 of every data URI is stored as `<N bytes base64 omitted>`.

### When the model cannot read a file

A model, or the route LiteLLM picks for it, may refuse a file type, and the files of the last visitor messages go
with every request: without a fallback one such file made every later turn fail (the staging bench: a PDF refused
with HTTP 500, a `.txt` with 400 and 422). So, contract 3.7:

- A request with file parts (inline images, or documents as `file` parts) that the backend answers with HTTP 400,
  413, 415, 422 or 500 is sent again **at once**: first without the file parts of the earlier visitor messages,
  then, when that is refused the same way and the newest visitor message still has a file part, without any file
  part. A request without file parts, and any other answer (401, 404, 502, 429 after its retries, ...), is handled
  as before.
- A file left out is its line `[附件] <name>（<mimetype>，<size>）：<signed url>`, followed by
  `[附件說明] <name>：模型無法讀取這個檔案，只附下載連結。`
- These requests are not retries: no wait, and the connection's *Retries* does not count them; HTTP 429 and 503 are
  still retried as usual. The connection's timeout bounds them all: none starts after the deadline of the answer.
- Each request writes its own call log (a refused one `http_error` with its HTTP status). The answer is the first
  one that succeeds; when none does, the visitor gets the error message, as before.
- **The conversation remembers**: the reply job keeps the ids of the files left out (*Files Sent as Download
  Links*, `im_livechat.ai.job.file_fallback_ids`), written before the next request is sent. Every later request of
  the conversation, a later run of the same job included, sends those files as their line and the note from the
  start, never as a file part again; they take nothing of the counts or of the 7 MiB.
- n8n is not concerned: it always gets signed links (`attachments`).

## n8n backend (`n8n`)

```http
POST {endpoint}
Content-Type: application/json
X-Woow-Timestamp: 1793430000
X-Woow-Signature: sha256=<hex HMAC-SHA256(secret, "<X-Woow-Timestamp>.<raw body>")>
```

There is never an `Authorization` header. The two signature headers are sent only when the connection
has a signing secret (its *Signing Secret*, stored like an API key and never sent). The body is compact
UTF-8 JSON, and those exact bytes are signed and sent:

```json
{
  "event": "message",
  "session_id": "woow-ai-0edfec80-42",
  "action": "sendMessage",
  "sessionId": "woow-ai-0edfec80-42",
  "chatInput": "請問營業時間？",
  "assistant": {"id": 5, "name": "官網助理"},
  "contact": {"id": 3, "name": "官網客服"},
  "user": {"user_id": null, "partner_id": null, "guest_id": 7, "name": "訪客", "email": "", "lang": "zh_TW",
           "tz": "Asia/Taipei", "is_portal": false, "is_guest": true, "company_id": null,
           "visitor_key": "guest:7"},
  "channel": {"id": 42, "type": "livechat",
              "livechat": {"livechat_channel_id": 3, "livechat_channel_name": "官網客服", "channel_id": 3,
                           "channel_name": "官網客服", "operator_name": "客服小明", "country_code": "TW",
                           "visitor_kind": "guest", "handoff_available": true}},
  "surface": {"type": "livechat", "livechat_channel_id": 3},
  "system_prompt": "你是官網的客服助理。",
  "message": {"id": 991, "text": "請問營業時間？", "attachments": []},
  "history": [{"role": "assistant", "text": "您好，有什麼可以幫您？", "attachments": []}],
  "events": [],
  "now": {"utc": "2026-11-28T02:20:33Z", "local": "2026-11-28T10:20:33+08:00", "tz": "Asia/Taipei",
          "weekday": "Saturday"}
}
```

- `assistant` is the AI assistant that answers: new workflows read it. `contact` is the Live Chat channel,
  kept for the workflows written for 18.0.1.x.
- `channel` is the conversation (`discuss.channel`); `channel.livechat` describes the Live Chat channel,
  its operator and the visitor kind (`guest`, `partner`, `public`, `employee` or `portal`).
  `handoff_available` is true when the channel has operators.
- `action`, `sessionId` and `chatInput` are the fields an n8n **Chat Trigger** reads (the same values as
  `session_id` and `message.text`).
- `message` is the visitor message being answered; `history` the rest of the context; `events` is always
  present (`[]` when there are none).
- `now` is the current time when the request is built: `utc` (`YYYY-MM-DDTHH:MM:SSZ`), `local` (the same instant
  in `tz`, with its offset), `tz` (an IANA name: the company's time zone, else the visitor's, else `UTC`) and
  `weekday` (the English name of the day of `local`). `user.tz` stays the visitor's own.
- `attachments` list the files of visitor messages as `{"id", "name", "mimetype", "size", "url",
  "expires_at"}`; nothing is inlined, the workflow downloads what it needs. AI and operator entries always
  have `[]`.
- Visitors are mostly anonymous: give the model public, read-only tools only, and tick 「可給客戶使用」 on
  the connection only after checking that the workflow looks up no company data, calls no tools and keeps
  no memory across conversations.

### Verifying the signature (required when a secret is set)

Verify the **raw bytes** (enable *Raw Body* on the Webhook node), refuse a timestamp more than 300 s away
and compare in constant time. Code node (needs `NODE_FUNCTION_ALLOW_BUILTIN=crypto`; keep the secret in an
environment variable, never in the workflow):

```js
const crypto = require('crypto');
const SECRET = $env.WOOW_AI_SIGNING_SECRET;
const item = $input.first();
const raw = (await this.helpers.getBinaryDataBuffer(0, 'data')).toString('utf8');
const h = Object.fromEntries(Object.entries(item.json.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
const ts = String(h['x-woow-timestamp'] || '');
if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > 300) throw new Error('stale');
const expected = 'sha256=' + crypto.createHmac('sha256', SECRET).update(ts + '.' + raw).digest('hex');
const a = Buffer.from(expected), b = Buffer.from(String(h['x-woow-signature'] || ''));
if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) throw new Error('bad signature');
return [{ json: JSON.parse(raw) }];
```

### Response

HTTP 200 with JSON, within the connection's timeout:

```json
{"reply": {"text": "markdown 文字"}}
{"reply": "text"}
{"replies": [{"text": "第一則"}, "第二則"]}
{"skip": true}
{"output": "markdown 文字"}
{"output": "好的，我幫您轉接真人客服，請稍候。", "handoff": true, "handoff_reason": "訪客要求專人"}
```

- `reply` posts one message, `replies` several (at most 10 are used), `output` (the answer of an n8n AI
  Agent or Chat Trigger) one, `skip` none: the job ends without a message. An n8n item list
  (`[{"output": ...}]`) is accepted: its first item is used.
- **Handoff**: `"handoff": true` (the JSON boolean, top level or in the first item) hands the visitor to a
  human: the reply text is posted first (`{"handoff": true}` alone posts the channel's handoff message),
  then Odoo pauses the AI, notifies the operators and waits for one. With `handoff`, `skip` is ignored.
  `handoff_reason` is optional: white space is collapsed and at most 300 characters are kept, shown to the
  operators and stored on the job. A Chat Trigger workflow, which can only answer `output`, ends it with a
  line `[[handoff]]` instead.
- `<think>` blocks and special tokens are removed, then every speaker label at the start of a reply text (see
  [Labels in answers](#labels-in-answers)). Replies are text only: reply attachments are ignored.
- Anything else is `invalid_response`: a non-JSON body, another shape, `replies` that is not a non-empty
  list, a reply that is not text, an empty reply without `handoff` or `skip`. A non-200 answer is an
  `http_error`. When the agent fails, answer non-200 (for example 502). The visitor then gets the
  assistant's error message and the operators are alerted.

### n8n Chat Trigger (hosted chat) workflows

A workflow that starts with a **Chat Trigger** node (public chat) can be used as it is:

- **Endpoint URL**: `<n8n>/webhook/<webhookId>/chat`. **Signing Secret** empty: a public Chat Trigger checks
  no signature.
- The Chat Trigger answers `{"output": "..."}`, posted as one message. Such agents may take 20 to 30 s per
  answer: keep the n8n default timeout (120 s).
- Limits: the workflow keeps **its own memory per `sessionId`** and ignores `history` and `events`, so it
  does not see what an operator wrote during a handoff, and it cannot verify that the request comes from
  Odoo. For new workflows prefer a **Webhook** node with the signature check.

### Minimal n8n workflow

1. **Webhook**: method `POST`, path e.g. `livechat-ai`, *Raw Body* on, respond with a *Respond to Webhook*
   node.
2. **Code**: the signature check above.
3. **IF**: `{{ $json.event }}` equals `message`; the false branch answers `{"skip": true}` (the ping of Test
   Connection).
4. **AI Agent**: prompt `{{ $json.message.text }}`; system message from `{{ $json.system_prompt }}` and
   `{{ $json.events }}`; optional memory keyed by `{{ $json.session_id }}`; public, read-only tools only.
5. **Respond to Webhook**: JSON `{{ JSON.stringify({ reply: { text: $json.output } }) }}`.

n8n keeps the webhook body (the conversation) with each execution: set an execution retention, or do not
save successful executions.

## Labels in answers

The history marks an operator's text with `[真人客服 <name>] ` (and Project AI labels people with `[客戶 <name>] ` or
`[負責同事 <name>] `). A model may start its own answer with such a label; posted as it is, the answer would look
written by that person (the staging bench: 「[真人客服 實測台｜你（管理員）] QA-SENSOR-02 的單價為 600 測試幣。」 after an
operator pause). Contract 3.14:

- Before a reply is posted, every label at its start is removed, with the white space after it (spaces, tabs,
  U+3000, line breaks), as long as one is there: `[<kind>]` or `[<kind> <text without ] or a line break>]`, where
  `<kind>` is 真人客服, 客戶 or 負責同事 followed by `]` or a space. So `[客戶服務中心] …` is not a label.
- It applies to every reply text: the LiteLLM answer (after `<think>` blocks and special tokens), and n8n `reply`,
  `replies` and `output`. A `[[handoff]]` marker is removed first, so a label after the marker is the start too.
- Nothing else changes: a label later in the text, or a leading bracket that is not such a label
  (「[注意] 截止日改了」), stays.
- A reply that held only labels is empty: `invalid_response` for LiteLLM, and for n8n a reply with no text (with
  `"handoff": true` the channel's handoff message is posted, as for an empty handoff answer).

## Test Connection (system administrators)

The button exists on the AI connection, on the AI assistant and on the channel's 「AI 助理」 tab. It never
runs the AI and shows only the HTTP status and one fixed sentence, never the upstream answer.

- **LiteLLM**: `GET {endpoint}/models` with the Bearer key. A warning is shown when the model is not in the
  answer's `data[].id` list. A connection without a model is refused before any call.
- **n8n**: the ping `{"event": "ping", "connection": {id, name}, "action": "loadPreviousSession",
  "sessionId": "woow-ai-ping-…"}`, signed like a message. From an assistant it adds `"assistant": {id,
  name}`; from a channel also the legacy `"contact"` (the channel). Any HTTP 200 is a success: answer it
  with `{"skip": true}`. A Chat Trigger answers `loadPreviousSession` itself, without running the agent.
- Connect timeout 10 s, read timeout 15 s, no redirects.

## Reply rendering

Every reply is treated as untrusted text: a small markdown subset (paragraphs, line breaks, bold, italics,
lists, tables, headings, code) is rendered after escaping, and the result is sanitized. Only `http(s)`
links to the site's own hosts (`web.base.url` and the websites' domains) become clickable; any other link,
a link with userinfo (`@`) or a backslash, and every other scheme stay plain text. Quotes and lists deeper
than 8 levels are rendered flat, so no reply can exhaust the recursion limit. A heading line longer than 2000
characters as written is shown as a paragraph; bold, italics and strike-through are skipped in a piece of a
line longer than 2000 characters counted after escaping (an `&` counts 5); code spans and links still work. A table has at most 30 columns (a wider one is shown as text) and
3000 cells (later rows are shown as text). Every step takes time linear in the reply.

## Signed download URLs

`GET {web.base.url}/im_livechat_ai/attachment/<id>/<expires>.<hex HMAC-SHA256(database.secret,
"im_livechat_ai|<id>|<checksum>|<expires>")>`, valid 1 hour. Only attachments of messages in a live chat
session are served, as a download (`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`,
`Cache-Control: private, no-store`); anything else is a 404. `web.base.url` must be the public address
of the site: the backend downloads from there.

## Call logs and usage (system administrators)

- One call log per answer (retries included), and one more for each request of the file fallback: status `ok`,
  `http_error`, `timeout`, `invalid_response` or `error`, the HTTP status, the duration, the tokens, the assistant,
  the connection, the conversation and the reply job. The error is the exception type name, never the upstream body or the key. Rows written
  by 18.0.1.x keep their statuses (`success`, `retry`).
- The request and response content is stored only while the connection's *Log Request Content* is on;
  turning it off erases what was stored. Call logs and finished reply jobs are deleted after 30 days.
- **AI Usage**: one permanent row per assistant, connection, Live Chat channel and UTC month: replies,
  failed replies, prompt, completion and total tokens (n8n reports no tokens: 0).

## When the AI cannot answer

When a channel's AI is on but its assistant is archived, not ticked 「可用在 Live Chat」, or its connection
is not ready (archived, no URL, a URL that is not allowed, no key or no model for LiteLLM):

- no reply job is created and no backend is called;
- the conversation gets one fixed notice per blocked period, posted as a notification (never part of the
  history, never answered);
- the channel's operators get one alert per blocked period (an OdooBot message and a toast), at most one per
  channel in 10 minutes; the alert is claimed with one update of the channel row, so concurrent requests and
  job threads never both alert (the row lock does not conflict with inserts that reference the channel). The
  alert that the caps refuse messages is a separate one with its own period and window. An alert that reached
  nobody (no operator found, every message failed) is not counted: the next message tries again;
- the server log gets a warning at most once every 10 minutes per channel and process.

The gate applies again when the reply job runs and before it posts the answer (contract 3.5): a job blocked
then makes no call, or posts no answer, is `skipped`, and the conversation gets the notice once per blocked
period. A job whose channel's AI was turned off is skipped quietly.

The blocked period ends when the AI answers again (a reply job is queued).

The notice is a notification: a session bridged to LINE does not forward it (the LINE bridge forwards comment
messages only), so a LINE visitor gets no notice; the operators still get the alert. The error message of a
failed reply is a comment and reaches LINE.

**Not in 18.0.2.0.0:** the "typing…" indicator while the backend works (contract 3.5 item 7): the AI
assistant's bot partner is not a member of the session, and Odoo 18 shows typing only for members. Planned for
18.0.2.0.1.
