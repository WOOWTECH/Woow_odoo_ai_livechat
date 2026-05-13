# Live Chat N8N Integration

Integrate Odoo 18 livechat with n8n workflow automation for intelligent automated responses.

## Features

- **Outbound Webhooks**: Automatically notify n8n when visitors send messages
- **Inbound Webhooks**: Receive and post automated replies from n8n workflows
- **Per-Channel Configuration**: Configure webhook settings independently for each livechat channel
- **Webhook Logging**: Track all webhook activity for debugging and monitoring
- **Error Handling**: Robust retry logic with exponential backoff (3 retries)
- **Security**: API key authentication, input validation, access control

## Requirements

- Odoo 18.0+
- `im_livechat` module (included in Odoo)
- n8n instance (self-hosted or cloud)

## Installation

1. Clone or download this module to your Odoo addons directory:
   ```bash
   cd /path/to/odoo/addons
   git clone https://github.com/WOOWTECH/im_livechat_n8n.git
   ```

2. Update the apps list in Odoo:
   - Go to Apps menu
   - Click "Update Apps List"

3. Install the module:
   - Search for "Live Chat N8N"
   - Click Install

## Quick Start

1. **Configure a Livechat Channel**:
   - Go to Website → Live Chat → Channels
   - Select a channel or create a new one
   - Go to the "N8N Integration" tab

2. **Enable Integration**:
   - Check "Enable N8N Integration"
   - Enter your n8n webhook URL
   - Copy the auto-generated API key

3. **Set up n8n Workflow**:
   - Create a webhook trigger node in n8n
   - Process the incoming chat message
   - Send response back to Odoo using HTTP Request node

4. **Test the Connection**:
   - Click "Test Webhook Connection" button
   - Verify the test appears in your n8n workflow

## Configuration

### Webhook URL
The URL of your n8n webhook endpoint. Example:
```
https://your-n8n-instance.com/webhook/livechat-handler
```

### API Key
Auto-generated secure key for authenticating n8n callbacks to Odoo.
- Use the "Regenerate API Key" button if needed
- Add this key to your n8n HTTP Request headers as `X-API-Key`

## API Reference

### Outbound Webhook Payload (Odoo → n8n)

When a visitor sends a message, this payload is sent to your n8n webhook:

```json
{
  "event_type": "message_received",
  "timestamp": "2024-01-15T10:30:00Z",
  "session": {
    "id": 123,
    "uuid": "abc-123-def",
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
    "api_key_header": "X-Odoo-Livechat-API-Key"
  }
}
```

### Inbound Webhook (n8n → Odoo)

**Endpoint**: `POST /im_livechat_n8n/webhook`

**Headers**:
- `Content-Type: application/json`
- `X-API-Key: your-api-key`

**Payload**:
```json
{
  "action": "send_message",
  "session_uuid": "abc-123-def",
  "message": {
    "body": "Thank you for contacting us! A support agent will assist you shortly.",
    "author_name": "Support Bot"
  }
}
```

**Response Codes**:
- `200`: Message sent successfully
- `400`: Invalid payload (missing fields, invalid UUID, message too large)
- `401`: Invalid or missing API key
- `404`: Session not found
- `500`: Server error

**Validation Rules**:
- Session UUID must be valid UUID format
- Message body required and cannot exceed 10KB
- API key must match a configured channel

## n8n Workflow Example

Here's a simple n8n workflow to respond to livechat messages:

1. **Webhook Trigger Node**:
   - Method: POST
   - Path: /livechat-handler

2. **Function Node** (Process Message):
   ```javascript
   const incomingMessage = $json.message.body;
   const sessionUuid = $json.session.uuid;

   // Your logic here (AI, FAQ lookup, etc.)
   const response = "Thank you for your message. An agent will help you shortly.";

   return {
     json: {
       session_uuid: sessionUuid,
       response: response
     }
   };
   ```

3. **HTTP Request Node** (Send Response):
   - Method: POST
   - URL: `{{ $json.metadata.callback_url }}` or `https://your-odoo.com/im_livechat_n8n/webhook`
   - Headers:
     - `Content-Type`: application/json
     - `X-API-Key`: your-api-key-from-odoo
   - Body:
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

## Webhook Logs

View webhook activity at:
- Website → Live Chat → Channels → [Channel] → "View Webhook Logs" button
- Or directly: Website → Configuration → Webhook Logs

Logs include:
- Timestamp
- Direction (outbound/inbound)
- Status (success/failed/timeout)
- Response time (milliseconds)
- HTTP status code
- Request/response payloads (for debugging)
- Error messages

**Log Retention**: Logs older than 30 days are automatically cleaned up by a scheduled action.

## Error Handling

### Outbound Webhooks (Odoo → n8n)
- **Retry Logic**: 3 automatic retries with exponential backoff (1s, 2s, 4s)
- **Timeout**: 10 seconds per attempt
- **Non-blocking**: Webhooks are sent in background threads to avoid delaying chat responses
- **Logging**: All failures are logged with error messages

### Inbound Webhooks (n8n → Odoo)
- **Validation**: Strict input validation (UUID format, message size, required fields)
- **Security**: API key authentication on all requests
- **Error Responses**: Clear error messages with appropriate HTTP status codes

## Troubleshooting

### Webhook not firing
1. Check "Enable N8N Integration" is checked
2. Verify webhook URL is correctly configured
3. Test connection using "Test Webhook Connection" button
4. Check webhook logs for error messages

### n8n not receiving webhooks
1. Verify n8n webhook URL is accessible from Odoo server
2. Check firewall/network rules
3. Review Odoo logs: `odoo.addons.im_livechat_n8n`
4. Test with public n8n cloud instance first

### Odoo not receiving responses
1. Verify API key matches between Odoo and n8n
2. Check session UUID is correct in response
3. Ensure `X-API-Key` header is included
4. Review webhook logs in Odoo

### Messages not appearing in chat
1. Verify session is still active
2. Check session UUID format is valid
3. Review inbound webhook logs for errors
4. Ensure message body is not empty

## Security Best Practices

1. **Use HTTPS**: Always use HTTPS URLs for production
2. **Rotate API Keys**: Regenerate keys periodically or if compromised
3. **Network Security**: Restrict webhook endpoints to known IP addresses if possible
4. **Message Validation**: Module automatically validates message size (10KB limit)
5. **Monitor Logs**: Regularly review webhook logs for suspicious activity

## Performance Considerations

- Webhooks are sent asynchronously (non-blocking)
- Maximum message size: 10KB
- Webhook timeout: 10 seconds
- Retry attempts: 3 with exponential backoff
- Log cleanup: Automatic (30-day retention)

## License

LGPL-3.0

## Support

For issues and feature requests, please visit:
https://github.com/WOOWTECH/im_livechat_n8n/issues

## Credits

Developed by **WOOWTECH**
