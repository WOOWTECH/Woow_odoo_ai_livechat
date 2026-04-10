# Live Chat N8N Integration - User Guide

## Table of Contents

1. [Introduction](#introduction)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Setting Up n8n Workflows](#setting-up-n8n-workflows)
5. [Testing the Integration](#testing-the-integration)
6. [Monitoring & Logs](#monitoring--logs)
7. [Troubleshooting](#troubleshooting)
8. [FAQ](#faq)

---

## Introduction

The **Live Chat N8N Integration** module connects Odoo 18's Live Chat system with n8n, a powerful workflow automation platform. This integration enables you to:

- Automatically trigger n8n workflows when visitors send messages
- Send automated AI-powered responses back to chat sessions
- Build complex chat automation workflows (FAQ bots, ticket creation, CRM updates, etc.)
- Track all webhook activity for monitoring and debugging

### How It Works

1. **Visitor sends message** → Odoo receives it in Live Chat
2. **Odoo triggers webhook** → Sends message data to n8n workflow
3. **n8n processes message** → Runs your custom workflow (AI, database lookup, etc.)
4. **n8n sends response** → Posts automated reply back to Odoo
5. **Visitor sees response** → Message appears in the chat window

### Use Cases

- **AI Chatbots**: Connect to ChatGPT, Claude, or other AI services for intelligent responses
- **FAQ Automation**: Answer common questions automatically before routing to agents
- **Lead Qualification**: Collect visitor information and create CRM leads automatically
- **Ticket Creation**: Generate support tickets in external systems
- **Multi-channel Notifications**: Alert your team via Slack, Discord, or email
- **Business Logic**: Execute complex workflows based on visitor messages

---

## Installation

### Prerequisites

Before installing this module, ensure you have:

1. **Odoo 18.0 or later** installed and running
2. **Live Chat module** installed (usually included by default)
3. **n8n instance** accessible from your Odoo server (cloud or self-hosted)

### Installation Steps

#### Step 1: Install the Module

1. Download or clone the module to your Odoo addons directory:
   ```bash
   cd /path/to/odoo/addons
   git clone https://github.com/WOOWTECH/im_livechat_n8n.git
   ```

2. Restart your Odoo server to detect the new module

3. Log in to Odoo as an administrator

4. Go to **Apps** menu

5. Click **Update Apps List**

6. Search for "Live Chat N8N"

7. Click **Install**

#### Step 2: Verify Installation

After installation, you should see:

- A new "N8N Integration" tab in Live Chat channel settings
- A new "Webhook Logs" menu item under Website → Configuration

---

## Configuration

### Configuring a Live Chat Channel

#### Step 1: Open Channel Settings

1. Go to **Live Chat** → **Channels**
2. Select an existing channel or create a new one
3. Click on the **N8N Integration** tab

#### Step 2: Enable Integration

1. Check the **Enable N8N Integration** checkbox
2. The **API Key** field will auto-populate with a secure random key
3. Copy and save this API key - you'll need it for n8n configuration

#### Step 3: Configure Webhook URL

1. Enter your n8n webhook URL in the **N8N Webhook URL** field

   Example URLs:
   - Cloud n8n: `https://your-instance.app.n8n.cloud/webhook/odoo-livechat`
   - Self-hosted: `https://n8n.yourdomain.com/webhook/odoo-livechat`
   - Local testing: `http://localhost:5678/webhook/odoo-livechat`

2. Click **Save**

**Important Notes**:
- Use HTTPS in production for security
- The module will warn you if using HTTP (except for localhost)
- Make sure the URL is accessible from your Odoo server

#### Step 4: Test Connection

1. Click the **Test Webhook Connection** button
2. You should see a success notification if everything is configured correctly
3. Check your n8n workflow to verify it received the test event

### API Key Management

#### Viewing the API Key

The API key is displayed in the **N8N Integration** tab. Keep this secure!

#### Regenerating the API Key

If you need to rotate your API key (recommended periodically):

1. Click **Regenerate API Key** button
2. Copy the new key
3. Update your n8n workflow with the new key
4. Old key will immediately stop working

**When to regenerate**:
- Regular security rotation (every 90 days recommended)
- If API key is compromised or exposed
- When removing access for a specific n8n workflow

---

## Setting Up n8n Workflows

### Basic Workflow Structure

A typical n8n workflow for Odoo Live Chat has three components:

1. **Webhook Trigger** - Receives messages from Odoo
2. **Processing Logic** - Your custom workflow (AI, database, etc.)
3. **HTTP Request** - Sends response back to Odoo

### Step-by-Step Workflow Creation

#### Step 1: Create Webhook Trigger

1. In n8n, create a new workflow
2. Add a **Webhook** node
3. Configure the webhook:
   - **HTTP Method**: POST
   - **Path**: Choose a unique path (e.g., `odoo-livechat`)
   - **Authentication**: None (we'll use API key in response)

4. Save and activate the workflow
5. Copy the webhook URL (e.g., `https://your-n8n.com/webhook/odoo-livechat`)
6. Paste this URL in your Odoo channel configuration

#### Step 2: Add Processing Logic

Example 1: **Simple Auto-Reply**

Add a **Set** node to create a static response:

```javascript
// Node: Set Response
{
  "response": "Thank you for contacting us! An agent will be with you shortly."
}
```

Example 2: **AI-Powered Response**

Add an **OpenAI** node (or similar):

1. Add **OpenAI** node
2. Configure with your API key
3. Use the incoming message: `{{ $json.message.body }}`
4. Get AI response and pass to next node

Example 3: **FAQ Database Lookup**

Add a **MySQL** or **HTTP Request** node to query your FAQ database:

```sql
SELECT answer FROM faq
WHERE LOWER(question) LIKE LOWER('%{{ $json.message.body }}%')
LIMIT 1
```

#### Step 3: Send Response to Odoo

Add an **HTTP Request** node configured as follows:

**Settings**:
- **Method**: POST
- **URL**: `{{ $json.metadata.callback_url }}` or your Odoo URL + `/im_livechat_n8n/webhook`

**Headers**:
```json
{
  "Content-Type": "application/json",
  "X-API-Key": "your-api-key-from-odoo"
}
```

**Body** (JSON):
```json
{
  "action": "send_message",
  "session_uuid": "{{ $json.session.uuid }}",
  "message": {
    "body": "{{ $json.response }}",
    "author_name": "Support Bot"
  }
}
```

**Tips**:
- Replace `your-api-key-from-odoo` with the actual API key from your Odoo channel
- Use expressions to reference data from previous nodes
- You can customize `author_name` (e.g., "AI Assistant", "FAQ Bot")

### Complete Workflow Examples

#### Example 1: Echo Bot

Simplest possible bot - echoes back what the visitor says:

```
[Webhook Trigger]
    ↓
[Function: Build Response]
    return {
      response: "You said: " + $json.message.body
    };
    ↓
[HTTP Request: Send to Odoo]
```

#### Example 2: AI Support Bot

Uses OpenAI to provide intelligent responses:

```
[Webhook Trigger]
    ↓
[OpenAI: Generate Response]
    Prompt: "You are a helpful customer support agent. Answer this: {{ $json.message.body }}"
    ↓
[Function: Format Response]
    ↓
[HTTP Request: Send to Odoo]
```

#### Example 3: Smart Router

Routes to different workflows based on keywords:

```
[Webhook Trigger]
    ↓
[Switch: Detect Intent]
    Case 1: Contains "price" → [Pricing Workflow]
    Case 2: Contains "support" → [Create Ticket]
    Case 3: Contains "sales" → [Notify Sales Team]
    Default: [Generic FAQ Response]
    ↓
[HTTP Request: Send to Odoo]
```

### Available Data in Webhooks

When Odoo sends a webhook to n8n, you receive this data:

```javascript
{
  event_type: "message_received",
  timestamp: "2024-01-15T10:30:00Z",
  session: {
    id: 123,                          // Session database ID
    uuid: "abc-123-def",              // Unique session identifier
    name: "Visitor #45",              // Session display name
    started_at: "2024-01-15T10:25:00Z",
    visitor_name: "John Doe",         // Visitor's name (if provided)
    visitor_country: "US",            // Country code
    visitor_lang: "en_US"             // Language code
  },
  message: {
    id: 456,                          // Message database ID
    body: "Hello, I need help",       // Message text
    author_id: 789,                   // Author partner ID
    author_name: "John Doe",          // Author display name
    author_type: "visitor",           // "visitor" or "operator"
    created_at: "2024-01-15T10:30:00Z"
  },
  channel: {
    id: 1,                            // Channel database ID
    name: "Website Support"           // Channel name
  },
  metadata: {
    odoo_base_url: "https://your-odoo.com",
    callback_url: "https://your-odoo.com/im_livechat_n8n/webhook",
    api_key_header: "X-Odoo-Livechat-API-Key"
  }
}
```

**Usage Examples**:

- `{{ $json.message.body }}` - Access the message text
- `{{ $json.session.uuid }}` - Get session UUID for response
- `{{ $json.visitor_name }}` - Personalize response with visitor name
- `{{ $json.metadata.callback_url }}` - Dynamic callback URL

---

## Testing the Integration

### Quick Test from Odoo

1. Go to your Live Chat channel settings
2. Click **Test Webhook Connection** button
3. Check the notification message
4. Verify the test event appears in your n8n workflow execution history

### Full End-to-End Test

#### Step 1: Start a Test Chat

1. Open your website with Live Chat enabled
2. Open the chat widget
3. Send a test message: "Hello, this is a test"

#### Step 2: Verify in n8n

1. Go to your n8n workflow
2. Check the **Executions** tab
3. You should see a new execution with the message data

#### Step 3: Verify Response in Odoo

If your workflow sends a response:

1. Check the chat widget
2. You should see the automated response appear
3. The message should show the author name you configured (e.g., "Support Bot")

### Common Test Scenarios

#### Test 1: Basic Connectivity
**Message**: "test"
**Expected**: Workflow triggers, shows message in n8n execution

#### Test 2: Response Delivery
**Setup**: Configure workflow to send a simple response
**Message**: "hello"
**Expected**: Automated reply appears in chat

#### Test 3: Error Handling
**Setup**: Temporarily disable webhook in n8n
**Message**: "test"
**Expected**: Error logged in Odoo webhook logs

---

## Monitoring & Logs

### Viewing Webhook Logs

#### From Channel Settings

1. Go to **Website** → **Live Chat** → **Channels**
2. Select your channel
3. Click **View Webhook Logs** button
4. Logs are filtered to show only this channel's activity

#### From Global Logs View

1. Go to **Website** → **Configuration** → **Webhook Logs**
2. View all webhook activity across all channels
3. Use filters to narrow down:
   - Direction (Outbound/Inbound)
   - Status (Success/Failed/Timeout)
   - Time period (Today, Last 7 days, etc.)

### Understanding Log Entries

Each log entry contains:

- **Timestamp**: When the webhook was triggered
- **Direction**:
  - **Outbound**: Odoo → n8n (visitor message sent to workflow)
  - **Inbound**: n8n → Odoo (automated response received)
- **Status**:
  - **Success**: Webhook completed successfully
  - **Failed**: Webhook failed (network error, invalid response, etc.)
  - **Timeout**: Webhook took longer than 10 seconds
- **Response Time**: How long the request took (in milliseconds)
- **HTTP Status**: Status code returned (200, 404, 500, etc.)
- **Request Payload**: The data sent (click to expand and view JSON)
- **Response Payload**: The response received
- **Error Message**: Details if the webhook failed

### Interpreting Common Log Patterns

#### Healthy System
- Most logs show "Success" status
- Response times under 2000ms
- HTTP status 200

#### Network Issues
- Many "Timeout" statuses
- High response times (>8000ms)
- Check network connectivity to n8n

#### Configuration Issues
- "Failed" statuses with 401 errors
- Error message: "Invalid API key"
- Solution: Verify API key matches in both systems

#### n8n Workflow Issues
- "Failed" statuses with 500 errors
- Check n8n workflow execution logs for errors
- Common issues: missing nodes, invalid expressions

### Log Retention

- Logs are kept for **30 days** by default
- Automatic cleanup runs daily via scheduled action
- Old logs are permanently deleted

**To modify retention period**:
1. Go to **Settings** → **Technical** → **Automation** → **Scheduled Actions**
2. Find "N8N Webhook Log Cleanup"
3. Modify the Python code if needed (change `timedelta(days=30)`)

---

## Troubleshooting

### Problem: Webhooks Not Firing

**Symptoms**: Messages sent in chat, but n8n workflow doesn't trigger

**Solutions**:

1. **Check Integration is Enabled**
   - Go to channel settings → N8N Integration tab
   - Verify "Enable N8N Integration" is checked

2. **Verify Webhook URL**
   - Check URL is correct and accessible
   - Test with curl:
     ```bash
     curl -X POST https://your-n8n.com/webhook/odoo-livechat \
       -H "Content-Type: application/json" \
       -d '{"test": true}'
     ```

3. **Check Firewall/Network**
   - Ensure Odoo server can reach n8n
   - Check firewall rules
   - Verify DNS resolution

4. **Review Odoo Logs**
   - Enable debug logging: `--log-level=debug`
   - Search for: `odoo.addons.im_livechat_n8n`

### Problem: n8n Not Receiving Webhooks

**Symptoms**: Odoo webhook logs show "Success", but n8n has no executions

**Solutions**:

1. **Check Webhook Path**
   - Verify path in n8n matches URL in Odoo
   - Path is case-sensitive

2. **Check Webhook is Active**
   - n8n workflow must be **activated**
   - Check workflow status toggle

3. **Review n8n Logs**
   - Check n8n execution logs for errors
   - Look for webhook authentication issues

### Problem: Responses Not Appearing in Chat

**Symptoms**: n8n workflow runs, but messages don't appear in Odoo chat

**Solutions**:

1. **Verify API Key**
   - Check API key in n8n HTTP Request node matches Odoo
   - Key is case-sensitive
   - No extra spaces

2. **Check Session UUID**
   - Ensure you're using correct UUID: `{{ $json.session.uuid }}`
   - UUID must be from the incoming webhook

3. **Verify Request Format**
   - Check Content-Type header: `application/json`
   - Check X-API-Key header is present
   - Verify JSON structure matches documentation

4. **Check Webhook Logs**
   - Go to Odoo webhook logs
   - Filter by "Inbound"
   - Check for error messages

5. **Check Session is Active**
   - Session may have been closed
   - Visitor may have left the chat

### Problem: High Response Times

**Symptoms**: Webhook logs show success but response times > 5000ms

**Solutions**:

1. **Optimize n8n Workflow**
   - Reduce external API calls
   - Use caching where possible
   - Simplify complex logic

2. **Check n8n Server Resources**
   - CPU/memory usage
   - Network latency
   - Database performance

3. **Use Async Processing**
   - For complex workflows, send immediate acknowledgment
   - Process in background
   - Send follow-up message when ready

### Problem: Messages Getting Cut Off

**Symptoms**: Long messages are truncated or rejected

**Solutions**:

1. **Check Message Size**
   - Maximum size: 10KB (10,240 bytes)
   - Split long messages into multiple parts

2. **Validate Encoding**
   - Ensure UTF-8 encoding
   - Watch for special characters

### Error Reference

| Error Code | Meaning | Solution |
|------------|---------|----------|
| 400 | Invalid payload | Check JSON structure, required fields |
| 401 | Invalid API key | Verify API key matches in Odoo and n8n |
| 404 | Session not found | Session may be closed or UUID incorrect |
| 500 | Server error | Check Odoo logs for stack trace |
| Timeout | Request took > 10s | Optimize n8n workflow or increase timeout |

---

## FAQ

### General Questions

**Q: Do I need a paid n8n account?**
A: No, n8n has a free self-hosted option. Cloud plans are also available.

**Q: Can I use this with multiple Live Chat channels?**
A: Yes! Each channel can have its own webhook URL and API key.

**Q: Are webhooks sent for operator messages?**
A: No, only visitor messages trigger webhooks to avoid loops.

**Q: What happens if n8n is down?**
A: Odoo will retry 3 times with exponential backoff. Chat continues to work normally.

**Q: Can I disable logging?**
A: Logging is built-in for debugging. You can set shorter retention periods.

### Technical Questions

**Q: Is this integration real-time?**
A: Yes, webhooks are triggered immediately when visitors send messages.

**Q: Are webhooks blocking?**
A: No, webhooks are sent in background threads to avoid delaying chat.

**Q: Can I send rich formatting in responses?**
A: Yes, use HTML in the message body (e.g., `<b>bold</b>`, `<i>italic</i>`).

**Q: How many retries on failure?**
A: 3 automatic retries with exponential backoff (1s, 2s, 4s delays).

**Q: What's the webhook timeout?**
A: 10 seconds per attempt, 30 seconds total with retries.

**Q: Can I send multiple responses per trigger?**
A: Yes, use the same session UUID in multiple HTTP requests.

### Security Questions

**Q: Is the API key secure?**
A: Yes, it's generated using cryptographically secure random tokens (32 bytes).

**Q: Should I use HTTPS?**
A: Absolutely! Always use HTTPS in production.

**Q: Can I restrict webhook access by IP?**
A: Yes, configure your firewall to only allow your n8n server IP.

**Q: How often should I rotate API keys?**
A: Recommended every 90 days or if compromised.

**Q: Are messages encrypted?**
A: When using HTTPS, all traffic is TLS-encrypted.

### Workflow Questions

**Q: Can I trigger multiple n8n workflows?**
A: Currently one webhook URL per channel, but you can route to multiple workflows in n8n.

**Q: Can I use conditional logic?**
A: Yes! Use n8n's Switch or IF nodes to route based on message content.

**Q: Can I integrate with AI services?**
A: Yes! n8n has nodes for OpenAI, Anthropic, Google AI, and more.

**Q: Can I create CRM leads from chats?**
A: Yes! Use n8n's HTTP Request node to call Odoo's API to create leads.

**Q: Can I send notifications to Slack/Discord?**
A: Yes! n8n has native nodes for most notification services.

---

## Additional Resources

- **n8n Documentation**: https://docs.n8n.io
- **Odoo Live Chat Documentation**: https://www.odoo.com/documentation/18.0/applications/websites/livechat.html
- **Module Repository**: https://github.com/WOOWTECH/im_livechat_n8n
- **Issue Tracker**: https://github.com/WOOWTECH/im_livechat_n8n/issues

---

**Need Help?**

If you encounter issues not covered in this guide:

1. Check the webhook logs for detailed error messages
2. Review n8n execution logs
3. Enable debug logging in Odoo
4. Open an issue on GitHub with logs and configuration details

**Happy Automating!**
