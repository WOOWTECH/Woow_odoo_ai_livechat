# project_goal.md

You are an Odoo 18 expert and project orchestrator.

**Project Objective:**
Develop a custom module for Odoo 18 called `im_livechat_n8n`. Integrate Odoo 18 Livechat with an n8n AI Agent workflow via Webhooks.
This will function as a "Headless RAG" system where Odoo handles the UI, and n8n handles the intelligence.

**Technical Architecture:**
1.  **Outgoing (Odoo -> n8n)**:
    - Trigger: New messages in `discuss.channel`.
    - Mechanism: Asynchronous Webhook (Threading).
    - Data: Sends channel_id (used as Session ID), `content`, `author_name`, and `tags`.
    - Constraint: Ignore attachments/files.
2.  **Incoming (n8n -> Odoo)**:
    - Trigger: HTTP Callback from n8n.
    - Mechanism: Custom Controller.
    - Action: Post the AI's response back to the channel using a specific AI Partner identity.

**The Critical Chain (Development Roadmap):**
- **Task 1 (Model Layer)**: Define Data Models (`im_livechat.channel` extension & Tags).
- **Task 2 (View Layer)**: Configure Backend UI for n8n settings.
- **Task 3 (The Bottleneck)**: Implement Outgoing Logic (Async Webhook & Filtering).
- **Task 4 (Interface Layer)**: Implement Incoming Controller (Callback API).
- **Task 5 (Integration)**: Finalize Manifest and Security.
