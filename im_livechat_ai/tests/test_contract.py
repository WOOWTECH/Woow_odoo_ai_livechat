# -*- coding: utf-8 -*-
"""Contract v1.2 (docs/CONTRACT.md, section 5): the fourteen shared checks of
tests/contract/harness.py, run on Live Chat AI.

Live Chat is the text-only product: whole (not streamed) LiteLLM replies, no
identity block in the system message, no speaker labels (the text as typed),
clickable links only to the site's own hosts, conversation events in the n8n
``events`` (CONTRACT.md 3.1). The hooks below map the harness on the module's
own code paths:

- one backend call: ``im_livechat.channel._generate_ai_reply`` for a session
  whose visitor message was created without the trigger;
- one turn: the visitor posts (the trigger queues the reply job in that
  transaction) and the job runs once in the test transaction; an earlier turn
  (``spec['history']``) is the visitor's message with the file, the
  assistant's answer and an operator's message (Live Chat's conversation
  event), created without the trigger;
- Test Connection: the button of the AI connection, or of the AI assistant;
- a post with a forged context (check 14): the visitor's message_post from a
  superuser environment with the context applied after sudo(), so only the
  module's own cleaning drops the ``default_*`` keys.

Never edit tests/contract/ here: a change is a new contract version.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.im_livechat_ai.models.ai_connection import NO_IN_USE_CHECK
from odoo.addons.im_livechat_ai.tools.backend import n8n_signature_headers
from odoo.addons.im_livechat_ai.tools.markdown import markdown_to_html

from .common import add_legacy_channel_columns, set_legacy_channel_values
from .contract.harness import ContractChecks

TEXT_FIELD_TYPES = ('char', 'text', 'html', 'selection')


@tagged('post_install', '-at_install', 'woow_ai_contract')
class TestContract(ContractChecks, TransactionCase):
    contract_module = 'im_livechat_ai'
    contract_surface_type = 'livechat'
    contract_session_prefix = 'woow-ai'
    contract_models = {
        'connection': 'im_livechat.ai.connection',
        'assistant': 'im_livechat.ai.assistant',
        'job': 'im_livechat.ai.job',
    }
    contract_text_only = True
    contract_streams = False
    contract_identity_block = False
    contract_link_policy = 'site_hosts'
    contract_speaker_label = False
    contract_events = True
    contract_person_has_email = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(cls.env, login='contract_operator', name='Contract Operator',
                                     email='contract.operator@example.com',
                                     groups='base.group_user,im_livechat.im_livechat_group_user')

    # --- records of a spec ---

    def _contract_records(self, spec, with_channel=True):
        """(AI connection, AI assistant, Live Chat channel or None) of a spec."""
        backend = spec['backend']
        vals = {
            'name': 'Contract %s' % backend,
            'backend_type': backend,
            'endpoint_url': spec['endpoint'],
            'log_payload': bool(spec['log_payload']),
        }
        if spec.get('key'):
            vals['api_key'] = spec['key']
        if backend == 'openai':
            vals['model'] = spec['model']
        if spec.get('timeout') is not None:
            vals['timeout_seconds'] = spec['timeout']
        if spec.get('retries') is not None:
            vals['max_retries'] = spec['retries']
        connection = self.env['im_livechat.ai.connection'].create(vals)
        assistant = self.env['im_livechat.ai.assistant'].create({
            'name': 'Contract Assistant',
            'connection_id': connection.id,
            'livechat_available': True,
            'system_prompt': spec['prompt'],
        })
        channel = None
        if with_channel:
            channel = self.env['im_livechat.channel'].create({
                'name': 'Contract Live Chat',
                'user_ids': [(4, self.operator.id)],
                'ai_assistant_id': assistant.id,
                'ai_enabled': True,
            })
        return connection, assistant, channel

    def _contract_session(self, channel, spec):
        """A session of `channel` (the operator's member first, as Odoo creates
        it) whose visitor is the person of the spec: a partner with an email."""
        person = self.env['res.partner'].create({'name': spec['person_name'], 'email': spec['person_email']})
        session = self.env['discuss.channel'].create({
            'name': 'Contract Session',
            'channel_type': 'livechat',
            'livechat_operator_id': self.operator.partner_id.id,
            'livechat_channel_id': channel.id,
            'channel_member_ids': [(0, 0, {'partner_id': self.operator.partner_id.id})],
        })
        self.env['discuss.channel.member'].create({'channel_id': session.id, 'partner_id': person.id})
        return session, person

    @staticmethod
    def _contract_post(session, person, text):
        """The visitor's post, through message_post: the trigger runs."""
        return session.message_post(body=text, author_id=person.id, message_type='comment',
                                    subtype_xmlid='mail.mt_comment')

    @staticmethod
    def _contract_texts(records):
        """Every text value of `records` (char, text, html and selection fields)."""
        texts = []
        for record in records:
            for name, field in record._fields.items():
                if field.type in TEXT_FIELD_TYPES and record[name]:
                    texts.append(str(record[name]))
        return texts

    def _contract_last_id(self, model):
        return self.env[model].sudo().with_context(active_test=False).search([], order='id desc', limit=1).id or 0

    # --- adapter hooks ---

    def contract_call(self, spec):
        _connection, _assistant, channel = self._contract_records(spec)
        session, person = self._contract_session(channel, spec)
        message = self.env['mail.message'].create({
            'body': spec['text'], 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': person.id,
        })
        first_log = self._contract_last_id('llm.api.log')
        result = channel._generate_ai_reply(session, session._ai_conversation(channel), trigger_message=message)
        log = self.env['llm.api.log'].search([('id', '>', first_log), ('livechat_channel_id', '=', channel.id)],
                                             order='id desc', limit=1)
        self.assertEqual(len(log), 1, 'one call log per answer')
        return {
            'replies': [{'text': text, 'attachments': []} for text in result['replies']],
            'skip': result['skip'],
            'handoff': result['handoff'],
            'handoff_reason': result['handoff_reason'],
            'status': log.status,
            'tokens': (log.prompt_tokens, log.completion_tokens, log.total_tokens),
        }

    def _contract_earlier_turn(self, channel, assistant, session, person, history):
        """The earlier turn of spec['history'], created without the trigger: the person's message with the
        file, the assistant's answer, then an operator's message, which Live Chat sends as a conversation event
        (an operator wrote in the conversation)."""
        Message = self.env['mail.message']
        earlier = Message.create({
            'body': history['user_text'], 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': person.id,
        })
        attachment = self.env['ir.attachment'].create({
            'name': history['attachment']['name'], 'mimetype': history['attachment']['mimetype'],
            'datas': history['attachment']['base64'], 'res_model': 'discuss.channel', 'res_id': session.id,
        })
        earlier.attachment_ids = [(4, attachment.id)]
        Message.create({
            'body': history['assistant_text'], 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': assistant.partner_id.id,
        })
        Message.create({
            'body': '請稍候，我們確認一下。', 'model': 'discuss.channel', 'res_id': session.id,
            'message_type': 'comment', 'author_id': self.operator.partner_id.id,
        })

    def contract_turn(self, spec):
        _connection, assistant, channel = self._contract_records(spec)
        session, person = self._contract_session(channel, spec)
        if spec.get('history'):
            self._contract_earlier_turn(channel, assistant, session, person, spec['history'])
        first_message = self._contract_last_id('mail.message')
        first_log = self._contract_last_id('llm.api.log')
        bus_before = len(self.env.cr.precommit.data.get('bus.bus.values', []))
        self._contract_post(session, person, spec['text'])
        jobs = self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])
        self.assertEqual(len(jobs), 1, 'the post queued one reply job')
        self.assertEqual(jobs.kind, 'reply')
        jobs._run_job(commit=False)
        self.env.invalidate_all()
        logs = self.env['llm.api.log'].search([('id', '>', first_log)])
        messages = self.env['mail.message'].sudo().search([('id', '>', first_message)])
        alerts = [entry.get('message') or '' for entry in
                  self.env.cr.precommit.data.get('bus.bus.values', [])[bus_before:]]
        return {
            'call_logs': self._contract_texts(logs),
            'job_errors': self._contract_texts(jobs),
            'messages': [str(message.body or '') for message in messages]
                        + [str(message.subject or '') for message in messages] + alerts,
        }

    def contract_sign(self, secret, body, timestamp):
        return n8n_signature_headers(secret, body, timestamp=timestamp)

    def contract_session_id(self, db_uuid, channel_id):
        self.env['ir.config_parameter'].sudo().set_param('database.uuid', db_uuid)
        return self.env['im_livechat.channel']._ai_session_id(self.env['discuss.channel'].browse(channel_id))

    def contract_test_connection(self, spec):
        connection, assistant, _channel = self._contract_records(spec, with_channel=False)
        if spec['from_assistant']:
            return assistant.action_test_connection()
        return connection.action_test_connection()

    def contract_render(self, text, link_hosts):
        if link_hosts is None:
            # the product's rule: the site's own hosts (web.base.url and the websites' domains)
            link_hosts = self.env['im_livechat.channel']._ai_link_hosts()
        return markdown_to_html(text, link_hosts=link_hosts)

    def contract_neutralize_seed(self):
        conv = self.contract_spec()
        litellm, _assistant, channel = self._contract_records(conv)
        n8n, _n8n_assistant, _none = self._contract_records(self.contract_spec(backend='n8n'), with_channel=False)
        n8n.customer_safe = True
        self.assertTrue(litellm.api_key_set and n8n.api_key_set and litellm.active and n8n.active)
        # an upgraded database still has the 18.0.1.x backend columns of the channels for one version
        add_legacy_channel_columns(self.env.cr)
        self.env.flush_all()
        set_legacy_channel_values(self.env.cr, channel.id, ai_endpoint_url='https://legacy.example.com/v1',
                                  ai_api_token_secret='dummy-contract-legacy-key')
        self._contract_neutralized_channel = channel

    def contract_neutralize_extra(self):
        channel_id = self._contract_neutralized_channel.id
        return [
            ('18.0.1.x channel URL and key cleared',
             'SELECT count(*) FROM im_livechat_channel '
             'WHERE ai_endpoint_url IS NOT NULL OR ai_api_token_secret IS NOT NULL'),
            ('the channel keeps its "Answer With AI" switch',
             'SELECT count(*) FROM im_livechat_channel WHERE id = %d AND NOT ai_enabled' % channel_id),
        ]

    def contract_gate_start(self):
        spec = self.contract_spec(backend='openai')
        connection, _assistant, channel = self._contract_records(spec)
        session, person = self._contract_session(channel, spec)
        return {
            'spec': spec, 'connection': connection, 'channel': channel, 'session': session, 'person': person,
            'first_message': self._contract_last_id('mail.message'), 'posted': self.env['mail.message'],
        }

    def contract_gate_block(self, handle, blocked):
        # A LiteLLM connection without a model is not ready. The "in use" guard
        # refuses that change while an AI-enabled channel answers with the
        # connection (an administrator turns the channel's AI off first): the
        # test steps over the guard to reach the state an upgraded database
        # can be in.
        connection = handle['connection'].with_context(**{NO_IN_USE_CHECK: True})
        connection.model = False if blocked else handle['spec']['model']
        self.assertEqual(connection.readiness, 'blocked' if blocked else 'ok')

    def contract_gate_run(self, handle):
        job = self.env['im_livechat.ai.job'].search([
            ('discuss_channel_id', '=', handle['session'].id), ('state', '=', 'pending')], order='id desc', limit=1)
        self.assertEqual(len(job), 1, 'a pending reply job')
        job._run_job(commit=False)

    def contract_gate_post(self, handle, text):
        handle['posted'] |= self._contract_post(handle['session'], handle['person'], text)

    def contract_post_with_context(self, spec, context):
        _connection, _assistant, channel = self._contract_records(spec)
        session, person = self._contract_session(channel, spec)
        session.sudo().with_context(**context).message_post(
            body=spec['text'], author_id=person.id, message_type='comment', subtype_xmlid='mail.mt_comment')
        return self.env['im_livechat.ai.job'].search([('discuss_channel_id', '=', session.id)])

    def contract_gate_state(self, handle):
        session = handle['session']
        jobs = self.env['im_livechat.ai.job'].search_count([('discuss_channel_id', '=', session.id)])
        notices = self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', session.id),
            ('id', '>', handle['first_message']), ('id', 'not in', handle['posted'].ids),
        ], order='id')
        return {'jobs': jobs, 'notices': notices.mapped('message_type')}

    # --- the fourteen checks (Odoo 18 collects only the test methods a class defines itself) ---

    def test_contract_01_answers(self):
        self.contract_check_01_answers()

    def test_contract_02_requests(self):
        self.contract_check_02_requests()

    def test_contract_03_vectors(self):
        self.contract_check_03_vectors()

    def test_contract_04_render(self):
        self.contract_check_04_render()

    def test_contract_05_glossary(self):
        self.contract_check_05_glossary()

    def test_contract_06_neutralize(self):
        self.contract_check_06_neutralize()

    def test_contract_07_sums(self):
        self.contract_check_07_sums()

    def test_contract_08_secrets(self):
        self.contract_check_08_secrets()

    def test_contract_09_customer_safe(self):
        self.contract_check_09_customer_safe()

    def test_contract_10_retries(self):
        self.contract_check_10_retries()

    def test_contract_11_gate(self):
        self.contract_check_11_gate()

    def test_contract_12_identity_block(self):
        self.contract_check_12_identity_block()

    def test_contract_13_connections(self):
        self.contract_check_13_connections()

    def test_contract_14_job_rows(self):
        self.contract_check_14_job_rows()
