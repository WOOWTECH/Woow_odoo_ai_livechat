# -*- coding: utf-8 -*-
"""Contract v1.1 checks, shared by Discuss AI, Project AI and Live Chat AI (docs/CONTRACT.md).

This file, ``__init__.py``, the four JSON fixtures, ``SHA256SUMS`` and ``READY`` are copied byte for byte into
``<module>/tests/contract/``; never edit them in a product repo (a change is a new contract version).

How a product uses it (its own ``tests/test_contract.py``, imported in ``tests/__init__.py``)::

    from odoo.tests import tagged
    from .contract.harness import ContractChecks

    @tagged('post_install', '-at_install', 'woow_ai_contract')
    class TestContract(ContractChecks, ProductCase):      # ProductCase: a TransactionCase
        contract_module = 'woow_ai_contact'
        ...                                                # the class attributes below
        def contract_call(self, spec): ...                 # the adapter hooks below

        def test_contract_01_answers(self):
            self.contract_check_01_answers()
        ...                                                # one wrapper per check, 01 to 13

Odoo 18 collects only the ``test*`` methods a test class defines itself, so the product writes the
thirteen one-line wrappers; check 07 fails when one is missing, skipped, or does not call its check.
Run them at the default or the info log level, never warn or error (check 08 reads every record).

Class attributes (the product sets them; check 07 compares the product's values with CONTRACT.md 3.1,
``requests.json`` "products"):

- ``contract_module``: technical name of the module (``woow_ai_contact``, ``woow_ai_task``, ``im_livechat_ai``)
- ``contract_surface_type``: ``surface.type`` (``discuss``, ``project_task``, ``livechat``)
- ``contract_session_prefix``: ``woow-ai`` or ``woow-project-ai``
- ``contract_models``: {'connection': ..., 'assistant': ..., 'job': ...} model names
- ``contract_text_only``: True when replies post text only (Live Chat): ``expect_text_only`` is used
- ``contract_streams``: True when LiteLLM answers are streamed (SSE cases and streamed requests)
- ``contract_identity_block``: True when the LiteLLM system message has an identity block
- ``contract_link_policy``: ``any_host`` (every http(s) link stays a link) or ``site_hosts`` (a host list)
- ``contract_speaker_label``: True when the product puts a speaker label before a person's text (3.14)
- ``contract_events``: True when the product records conversation events (n8n ``events``)
- ``contract_person_has_email``: True when the n8n ``user.email`` of the canned person is its email
- ``contract_post_target``, ``contract_get_target``, ``contract_sleep_target``: what ``unittest.mock.patch``
  replaces (defaults ``requests.post``, ``requests.get``, ``time.sleep``)

Adapter hooks (the product implements them; ``spec`` is a dict, see ``contract_spec``):

- ``contract_call(spec)``: run ONE call of the product's own backend function for a conversation of
  ``spec`` (no reply posted). The harness has patched requests.post. Return a dict: ``replies`` (list of
  {'text', 'attachments'}), ``skip``, ``handoff``, ``handoff_reason``, ``status`` (the call log status:
  ok, http_error, timeout, invalid_response, error) and ``tokens`` ((prompt, completion, total)).
- ``contract_turn(spec)``: run ONE whole turn: the person of ``spec`` posts ``spec['text']`` and the
  product's reply job runs once in the test transaction. With ``spec['history']`` (requests.json
  "history"), first the person posts ``user_text`` with the attachment, the assistant's answer
  ``assistant_text`` is posted as the assistant, and a product with ``contract_events`` records one
  conversation event of its own kind; still one backend call. Return a dict of lists of str: ``call_logs``
  (every text field of the call logs written), ``job_errors``, ``messages`` (every message and staff alert
  the turn created: bodies, notices, activity notes).
- ``contract_speaker_kind(spec)``: products with ``contract_speaker_label`` only: the kind of speaker the
  label of the canned person starts with (one word, e.g. 客戶), or '' on a surface where every speaker is
  an employee and labels carry the name only.
- ``contract_sign(secret, body, timestamp)``: the n8n signature headers of ``body`` (bytes).
- ``contract_session_id(db_uuid, channel_id)``: the session id of discuss.channel ``channel_id`` when the
  database uuid is ``db_uuid``.
- ``contract_test_connection(spec)``: run Test Connection of a connection of ``spec`` (from its assistant
  when ``spec['from_assistant']``).
- ``contract_render(text, link_hosts)``: the HTML a reply ``text`` is rendered to (``link_hosts`` None:
  the product's default rule).
- ``contract_identity_text(name)``: the identity block of the LiteLLM system message for a person named
  ``name`` (products with an identity block only).
- ``contract_neutralize_seed()``: create connections (LiteLLM and n8n, with URL, key, active, n8n ticked
  safe for customers) and assistants (with a push key where the product has push).
- ``contract_neutralize_extra()``: optional, a list of (label, SQL counting rows that are still wrong).
- ``contract_gate_start()`` -> handle, ``contract_gate_block(handle, blocked)``,
  ``contract_gate_post(handle, text)``, ``contract_gate_state(handle)`` -> {'jobs': int, 'notices':
  [message_type, ...]} (jobs of the conversation; messages the product posted about the block),
  ``contract_gate_run(handle)``: run the conversation's pending reply job once, in the test transaction.

Requests: the harness reads the body a product passes to ``requests.post``: ``data=`` bytes (n8n must
send the signed bytes that way), or for LiteLLM also ``json=`` (serialized as requests does). A ``str``
body fails: requests does not send it as UTF-8 in every version.
"""

import base64
import hashlib
import hmac
import html as html_lib
import inspect
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from odoo.tools.misc import file_open

CONTRACT_VERSION = 'v1.1'
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = ('answers', 'requests', 'render', 'glossary')
CHECKS = ('01_answers', '02_requests', '03_vectors', '04_render', '05_glossary', '06_neutralize', '07_sums',
          '08_secrets', '09_customer_safe', '10_retries', '11_gate', '12_identity_block', '13_connections')
PRODUCT_FLAGS = ('surface_type', 'session_prefix', 'text_only', 'streams', 'identity_block', 'link_policy',
                 'speaker_label', 'events')
SECRET_KEY = 'sk-contract-secret-7f3a9c'
SECRET_BODY = 'UPSTREAM-BODY-7f3a9c'

_logger = logging.getLogger(__name__)
_fixtures = {}


def contract_fixture(name):
    """One of the four fixtures (parsed once per process)."""
    if name not in _fixtures:
        with open(os.path.join(HERE, '%s.json' % name), encoding='utf-8') as fp:
            _fixtures[name] = json.load(fp)
    return _fixtures[name]


def one_line_name(name):
    """The contract's name rule (3.7, 3.14): one line (white space collapsed), brackets turned into
    parentheses, at most 80 characters; a name with an '@' counts as no name."""
    name = ' '.join((name or '').replace('[', '(').replace(']', ')').split())
    return '' if '@' in name else name[:80]


def _json_error(doc):
    try:
        return requests.exceptions.JSONDecodeError('Expecting value', doc, 0)
    except Exception:  # noqa: BLE001 - older requests
        return ValueError('Expecting value')


class FakeResponse:
    """What a mocked requests.post / requests.get returns: a JSON body, a non-JSON body (``raw``) or a
    server-sent event stream (``chunks``, str pieces of the raw body)."""

    def __init__(self, status=200, body=None, raw=None, chunks=None, headers=None):
        self.status_code = status
        self.reason = 'OK' if status == 200 else 'ERROR'
        self.url = None
        self.raw = None
        self.encoding = 'utf-8'
        self._json = body
        if chunks is not None:
            self._chunks = [c.encode('utf-8') if isinstance(c, str) else c for c in chunks]
            content_type = 'text/event-stream; charset=utf-8'
        elif raw is not None:
            self._chunks = [raw.encode('utf-8')]
            self._json = _json_error(raw)
            content_type = 'text/html; charset=utf-8'
        else:
            self._chunks = [json.dumps(body, ensure_ascii=False).encode('utf-8')]
            content_type = 'application/json'
        self.headers = CaseInsensitiveDict({'Content-Type': content_type})
        self.headers.update(headers or {})
        self.content = b''.join(self._chunks)
        self.text = self.content.decode('utf-8', 'replace')
        self.closed = False
        self.elapsed = timedelta(0)
        self.history = []
        self.request = None
        self.is_redirect = 300 <= status < 400 and 'Location' in self.headers

    @property
    def ok(self):
        return self.status_code < 400

    def json(self, **_kwargs):
        if isinstance(self._json, Exception):
            raise self._json
        if self._chunks and self.headers.get('Content-Type', '').startswith('text/event-stream'):
            raise _json_error(self.text)
        return json.loads(self.content.decode('utf-8'))

    def iter_content(self, chunk_size=1, decode_unicode=False):
        for chunk in self._chunks:
            yield chunk.decode('utf-8', 'replace') if decode_unicode else chunk

    def iter_lines(self, chunk_size=512, decode_unicode=False, delimiter=None):
        buffer = b''
        for chunk in self._chunks:
            buffer += chunk
            *lines, buffer = buffer.split(b'\n')
            for line in lines:
                line = line.rstrip(b'\r')
                yield line.decode('utf-8', 'replace') if decode_unicode else line
        if buffer:
            yield buffer.decode('utf-8', 'replace') if decode_unicode else buffer

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('HTTP %s' % self.status_code, response=self)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _refused():
    """The connection could not be opened: the request was not sent."""
    return requests.ConnectionError(MaxRetryError(None, '/v1/chat/completions',
                                                  NewConnectionError(None, 'Connection refused')))


def _reset_after_send():
    """The backend received the request, then reset the connection."""
    return requests.ConnectionError(ProtocolError('Connection aborted.', ConnectionResetError(104, 'reset')))


def _post_call(mock):
    """(url, kwargs, data) of a mocked requests.post / requests.get call: ``data`` as the product passed it."""
    call = mock.call_args
    url = call.args[0] if call.args else call.kwargs.get('url')
    data = call.kwargs.get('data')
    if data is None and len(call.args) > 1:
        data = call.args[1]
    return url, call.kwargs, data


def _turn_text(turn):
    content = turn.get('content')
    if isinstance(content, list):
        return '\n'.join(part.get('text') or '' for part in content if isinstance(part, dict))
    return content or ''


def _parse_time(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def _lower_headers(kwargs):
    return {str(k).lower(): v for k, v in (kwargs.get('headers') or {}).items()}


def _hrefs(html):
    return [html_lib.unescape(href) for href in re.findall(r'<a\b[^>]*?\bhref="([^"]*)"', str(html or ''))]


def _sql_statements(sql):
    """Top-level statements of a neutralize.sql (comments removed, $$ bodies kept whole)."""
    text = re.sub(r'--[^\n]*', '', sql)
    statements, current, quoted = [], [], False
    for part in re.split(r'(\$\$|;)', text):
        if part == '$$':
            quoted = not quoted
            current.append(part)
        elif part == ';' and not quoted:
            statements.append(''.join(current).strip())
            current = []
        else:
            current.append(part)
    statements.append(''.join(current).strip())
    return [s for s in statements if s]


def _type_ok(value, kind):
    allowed = kind.split('|')
    checks = {
        'str': lambda v: isinstance(v, str),
        'int': lambda v: isinstance(v, int) and not isinstance(v, bool),
        'bool': lambda v: isinstance(v, bool),
        'null': lambda v: v is None,
        'object': lambda v: isinstance(v, dict),
        'list': lambda v: isinstance(v, list),
    }
    return any(checks[k](value) for k in allowed)


class ContractChecks:
    """Mixin with the twelve contract checks; see the module docstring."""

    contract_module = None
    contract_surface_type = None
    contract_session_prefix = None
    contract_models = None
    contract_text_only = False
    contract_streams = True
    contract_identity_block = True
    contract_link_policy = 'any_host'
    contract_speaker_label = False
    contract_events = False
    contract_person_has_email = True
    contract_post_target = 'requests.post'
    contract_get_target = 'requests.get'
    contract_sleep_target = 'time.sleep'

    # --- adapter hooks (the product implements them) ---

    def contract_call(self, spec):
        raise NotImplementedError

    def contract_turn(self, spec):
        raise NotImplementedError

    def contract_sign(self, secret, body, timestamp):
        raise NotImplementedError

    def contract_session_id(self, db_uuid, channel_id):
        raise NotImplementedError

    def contract_test_connection(self, spec):
        raise NotImplementedError

    def contract_render(self, text, link_hosts):
        raise NotImplementedError

    def contract_identity_text(self, name):
        raise NotImplementedError

    def contract_speaker_kind(self, spec):
        raise NotImplementedError

    def contract_neutralize_seed(self):
        raise NotImplementedError

    def contract_neutralize_extra(self):
        return []

    def contract_gate_start(self):
        raise NotImplementedError

    def contract_gate_block(self, handle, blocked):
        raise NotImplementedError

    def contract_gate_post(self, handle, text):
        raise NotImplementedError

    def contract_gate_state(self, handle):
        raise NotImplementedError

    def contract_gate_run(self, handle):
        raise NotImplementedError

    # --- helpers ---

    def contract_spec(self, **values):
        """A call or turn spec: the canned conversation of requests.json, overridden by ``values``.

        Keys: backend ('openai' or 'n8n'), stream, endpoint, key (LiteLLM API key, or n8n signing
        secret; None: unsigned), model, timeout (timeout_seconds, None: default), retries (max_retries,
        None: default), log_payload, prompt, text, person_name, person_email, from_assistant, history (None,
        or requests.json "history": an earlier turn)."""
        conv = contract_fixture('requests')['conversation']
        backend = values.get('backend', 'openai')
        spec = {
            'backend': backend,
            'stream': False,
            'endpoint': conv['endpoint_litellm'] if backend == 'openai' else conv['endpoint_n8n'],
            'key': conv['api_key'] if backend == 'openai' else conv['secret'],
            'model': conv['model'],
            'timeout': None,
            'retries': None,
            'log_payload': False,
            'prompt': conv['prompt'],
            'text': conv['text'],
            'person_name': conv['person_name'],
            'person_email': conv['person_email'],
            'from_assistant': False,
            'history': None,
        }
        spec.update(values)
        if not self.contract_streams:
            spec['stream'] = False
        return spec

    def _contract_patch_post(self, **kwargs):
        return patch(self.contract_post_target, **kwargs)

    def _contract_patch_sleep(self):
        return patch(self.contract_sleep_target)

    @staticmethod
    def _contract_response(case, stream=False):
        if 'chunks' in case:
            return FakeResponse(case.get('status', 200), chunks=case['chunks'])
        if 'raw' in case:
            return FakeResponse(case.get('status', 200), raw=case['raw'])
        return FakeResponse(case.get('status', 200), body=case['body'])

    @staticmethod
    def _contract_normalize(result, with_tokens=False):
        if (result.get('status') or 'ok') != 'ok':
            return {'error': result.get('status')}
        replies = result.get('replies') or []
        normalized = {
            'texts': [reply.get('text') or '' for reply in replies],
            'attachments': sum(len(reply.get('attachments') or []) for reply in replies),
            'skip': bool(result.get('skip')),
            'handoff': bool(result.get('handoff')),
            'reason': result.get('handoff_reason') or None,
        }
        if with_tokens:
            normalized['tokens'] = list(result.get('tokens') or (0, 0, 0))
        return normalized

    def _contract_expect(self, case):
        if self.contract_text_only and 'expect_text_only' in case:
            return case['expect_text_only']
        return case['expect']

    def _contract_body(self, kwargs, data, backend):
        """The bytes a request sends (3.6, 3.7): n8n ``data=`` bytes only (the signed bytes, sent as they are);
        LiteLLM ``data=`` bytes, or ``json=`` serialized as requests does. Never a str."""
        if backend == 'openai' and data is None and kwargs.get('json') is not None:
            return requests.Request('POST', 'https://example.invalid/', json=kwargs['json']).prepare().body
        self.assertIsInstance(data, bytes, 'the %s body is sent as bytes (data=), not %s' % (
            'n8n' if backend == 'n8n' else 'LiteLLM', type(data).__name__))
        return data

    def _contract_expected_text(self, spec):
        """message.text / chatInput / the last LiteLLM user turn: the text as typed, after the speaker label
        in a labelling product (3.14)."""
        if not self.contract_speaker_label:
            return spec['text']
        return self._contract_label(spec) + spec['text']

    def _contract_label(self, spec):
        """The label of the canned person (3.14): ``[<kind> <name>] ``, ``[<kind>] `` without a name; on a
        surface where every speaker is an employee the kind may be left out: ``[<name>] ``, or no label."""
        kind = self.contract_speaker_kind(spec) or ''
        self.assertTrue(len(kind.split()) <= 1, 'the kind of speaker is one word: %r' % kind)
        parts = [part for part in (kind, one_line_name(spec['person_name'])) if part]
        return '[%s] ' % ' '.join(parts) if parts else ''

    def _contract_session_pattern(self):
        uuid = (self.env['ir.config_parameter'].sudo().get_param('database.uuid') or '').replace('-', '')[:8]
        return re.compile(r'%s-%s-(\d+)' % (re.escape(self.contract_session_prefix), re.escape(uuid)))

    def _contract_check_keys(self, label, data, keys, extra=()):
        self.assertIsInstance(data, dict, label)
        missing = set(keys) - set(data)
        self.assertFalse(missing, '%s: missing keys %s' % (label, sorted(missing)))
        unknown = set(data) - set(keys) - set(extra)
        self.assertFalse(unknown, '%s: keys outside the contract %s' % (label, sorted(unknown)))
        for key, kind in keys.items():
            self.assertTrue(_type_ok(data[key], kind), '%s.%s: %r is not %s' % (label, key, data[key], kind))

    # --- 01: answers ---

    def contract_check_01_answers(self):
        answers = contract_fixture('answers')
        groups = [('n8n', 'n8n', False), ('openai', 'openai', False)]
        if self.contract_streams:
            groups.append(('sse', 'openai', True))
        count = 0
        for group, backend, stream in groups:
            for case in answers[group]:
                expect = self._contract_expect(case)
                with self.subTest(group=group, case=case['id']):
                    response = self._contract_response(case, stream)
                    with self._contract_patch_post(return_value=response), self._contract_patch_sleep():
                        result = self.contract_call(self.contract_spec(backend=backend, stream=stream))
                    self.assertEqual(self._contract_normalize(result, with_tokens='tokens' in expect), expect,
                                     '%s/%s' % (group, case['id']))
                count += 1
        self.assertGreaterEqual(count, len(answers['n8n']) + len(answers['openai']))

    # --- 02: request rules (canned conversation and ping) ---

    def _contract_turn_capture(self, spec, response):
        with self._contract_patch_post(return_value=response) as post, self._contract_patch_sleep():
            self.contract_turn(spec)
        self.assertEqual(post.call_count, 1, 'one backend call per turn')
        return _post_call(post)

    def contract_check_02_requests(self):
        rules = contract_fixture('requests')
        # n8n, signed, then without a signing secret (unsigned)
        for key in (rules['conversation']['secret'], None):
            spec = self.contract_spec(backend='n8n', key=key)
            url, kwargs, data = self._contract_turn_capture(spec, FakeResponse(200, body=rules['answers']['n8n']))
            self._contract_check_n8n_request(rules, spec, url, kwargs, data)
        # LiteLLM, streamed where the product streams, then not streamed
        for stream in ((True, False) if self.contract_streams else (False,)):
            spec = self.contract_spec(backend='openai', stream=stream)
            answer = FakeResponse(200, chunks=rules['answers']['litellm_stream']) if stream \
                else FakeResponse(200, body=rules['answers']['litellm'])
            url, kwargs, data = self._contract_turn_capture(spec, answer)
            self._contract_check_litellm_request(rules, spec, url, kwargs, data)
        # an earlier turn: the n8n history, ATT and events shapes, and the LiteLLM turns
        spec = self.contract_spec(backend='n8n', history=rules['history'])
        url, kwargs, data = self._contract_turn_capture(spec, FakeResponse(200, body=rules['answers']['n8n']))
        history_payload = self._contract_check_n8n_request(rules, spec, url, kwargs, data)
        self._contract_check_n8n_history(rules, spec, history_payload)
        spec = self.contract_spec(backend='openai', history=rules['history'])
        url, kwargs, data = self._contract_turn_capture(spec, FakeResponse(200, body=rules['answers']['litellm']))
        payload = self._contract_check_litellm_request(rules, spec, url, kwargs, data)
        self._contract_check_litellm_history(rules, spec, payload)
        # speaker labels (3.14): the same text on every path, a typed prefix kept after the label
        for case in rules['speaker_label']['cases']:
            with self.subTest(speaker=case['id']):
                for backend, answer in (('n8n', rules['answers']['n8n']), ('openai', rules['answers']['litellm'])):
                    spec = self.contract_spec(backend=backend, person_name=case['person_name'], text=case['text'])
                    url, kwargs, data = self._contract_turn_capture(spec, FakeResponse(200, body=answer))
                    if backend == 'n8n':
                        self._contract_check_n8n_request(rules, spec, url, kwargs, data)
                    else:
                        self._contract_check_litellm_request(rules, spec, url, kwargs, data)
        # the earlier turn's conversation events, last: a missing event hides no other result
        if self.contract_events:
            self.assertTrue(history_payload['events'], 'the product\'s conversation event is sent')
        else:
            self.assertEqual(history_payload['events'], [], 'no conversation events in this product')
        # Test Connection: the n8n ping (from the connection, from its assistant) and LiteLLM GET /models
        ping = rules['ping']
        for from_assistant in (False, True):
            spec = self.contract_spec(backend='n8n', from_assistant=from_assistant)
            with self._contract_patch_post(return_value=FakeResponse(200, body={})) as post, \
                    patch(self.contract_get_target) as get:
                self.contract_test_connection(spec)
            self.assertFalse(get.called)
            self.assertEqual(post.call_count, 1)
            url, kwargs, data = _post_call(post)
            body = self._contract_body(kwargs, data, 'n8n')
            headers = _lower_headers(kwargs)
            self.assertEqual(url, spec['endpoint'])
            self.assertNotIn('authorization', headers)
            self._contract_check_signature(spec['key'], headers, body)
            self.assertIs(kwargs.get('allow_redirects'), False)
            self.assertEqual(tuple(kwargs.get('timeout') or ()), tuple(ping['timeout']))
            payload = json.loads(body.decode('utf-8'))
            keys = list(ping['n8n']['keys']) + (list(ping['n8n']['from_assistant_keys']) if from_assistant else [])
            extra = ping['n8n']['allowed_extra_keys'].get(self.contract_surface_type, []) if from_assistant else []
            self.assertEqual(set(payload) - set(extra), set(keys), 'ping keys')
            for key, value in ping['n8n']['values'].items():
                self.assertEqual(payload[key], value)
            self.assertTrue(payload['sessionId'].startswith(self.contract_session_prefix + ping['n8n']['session_infix']),
                            payload['sessionId'])
            for key in ('connection',) + (('assistant',) if from_assistant else ()):
                self.assertEqual(set(payload[key]), set(ping['n8n']['id_name_keys']), key)
        spec = self.contract_spec(backend='openai')
        with patch(self.contract_get_target, return_value=FakeResponse(200, body=rules['answers']['models'])) as get, \
                self._contract_patch_post() as post:
            self.contract_test_connection(spec)
        self.assertFalse(post.called, 'Test Connection never runs a chat completion')
        self.assertEqual(get.call_count, 1)
        url, kwargs, _body = _post_call(get)
        self.assertEqual(url, spec['endpoint'].rstrip('/') + ping['litellm']['path_suffix'])
        self.assertEqual(_lower_headers(kwargs).get('authorization'), ping['litellm']['authorization'].format(key=spec['key']))
        self.assertIs(kwargs.get('allow_redirects'), False)
        self.assertEqual(tuple(kwargs.get('timeout') or ()), tuple(ping['timeout']))

    def _contract_check_signature(self, secret, headers, body):
        names = [name.lower() for name in contract_fixture('requests')['n8n']['signature_headers']]
        if not secret:
            self.assertFalse(set(names) & set(headers), 'unsigned without a signing secret')
            return
        timestamp = headers.get(names[0])
        self.assertTrue(timestamp and timestamp.isdigit(), timestamp)
        expected = 'sha256=' + hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        self.assertEqual(headers.get(names[1]), expected)

    def _contract_check_n8n_request(self, rules, spec, url, kwargs, data):
        n8n = rules['n8n']
        surface = self.contract_surface_type
        headers = _lower_headers(kwargs)
        self.assertEqual(url, spec['endpoint'])
        self.assertTrue((headers.get('content-type') or '').startswith(n8n['content_type']))
        for name in n8n['forbidden_headers']:
            self.assertNotIn(name.lower(), headers, 'never an %s header for n8n' % name)
        self.assertIsNone(kwargs.get('json'), 'n8n: the signed bytes are sent as data=, never json=')
        body = self._contract_body(kwargs, data, 'n8n')
        self._contract_check_signature(spec['key'], headers, body)
        self.assertIs(kwargs.get('allow_redirects'), False)
        payload = json.loads(body.decode('utf-8'))
        self.assertEqual(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), body,
                         'compact UTF-8 JSON')
        self._contract_check_keys('body', payload, n8n['body_keys'], n8n['allowed_extra_body_keys'].get(surface, []))
        for key, value in n8n['body_values'].items():
            self.assertEqual(payload[key], value, key)
        match = self._contract_session_pattern().fullmatch(payload['session_id'])
        self.assertTrue(match, payload['session_id'])
        self.assertEqual(payload['sessionId'], payload['session_id'])
        self._contract_check_keys('assistant', payload['assistant'], n8n['assistant_keys'])
        self._contract_check_keys('user', payload['user'], n8n['user_keys'], n8n['allowed_extra_user_keys'].get(surface, []))
        self.assertEqual(payload['user']['name'], '' if '@' in spec['person_name'] else spec['person_name'],
                         'the person\'s name (a name with an @ counts as no name)')
        if self.contract_person_has_email:
            self.assertEqual(payload['user']['email'], spec['person_email'], 'n8n receives the email')
        self._contract_check_keys('channel', payload['channel'], n8n['channel_keys'],
                                  set(payload['channel']) - set(n8n['channel_keys']))
        self.assertEqual(int(match.group(1)), payload['channel']['id'], 'session id = the conversation id')
        self._contract_check_keys('surface', payload['surface'], n8n['surface_keys'],
                                  set(payload['surface']) - set(n8n['surface_keys']))
        self.assertEqual(payload['surface']['type'], surface)
        self.assertTrue(payload['system_prompt'].startswith(spec['prompt']), payload['system_prompt'][:80])
        self._contract_check_keys('message', payload['message'], n8n['message_keys'])
        self.assertEqual(payload['message']['text'], self._contract_expected_text(spec), 'message.text')
        self.assertEqual(payload['chatInput'], payload['message']['text'])
        kind = self.contract_speaker_kind(spec) if self.contract_speaker_label else None
        if kind:
            self.assertIn('[' + kind, payload['system_prompt'], 'the system prompt explains the labels')
        entries = [payload['message']] + payload['history']
        for entry in payload['history']:
            self._contract_check_keys('history', entry, n8n['history_keys'])
        for entry in entries:
            for attachment in entry['attachments']:
                self._contract_check_keys('attachment', attachment, n8n['attachment_keys'])
        for event in payload['events']:
            self._contract_check_keys('event', event, n8n['event_keys'])
            self.assertRegex(event['time'], n8n['time_pattern'], 'events[].time is ISO 8601 UTC')
        return payload

    def _contract_check_n8n_history(self, rules, spec, payload):
        history, n8n = rules['history'], rules['n8n']
        entries = payload['history']
        users = [i for i, entry in enumerate(entries) if entry['role'] == 'user' and history['user_text'] in entry['text']]
        answers = [i for i, entry in enumerate(entries)
                   if entry['role'] == 'assistant' and entry['text'] == history['assistant_text']]
        self.assertTrue(users, 'the earlier message is in the history')
        self.assertTrue(answers, 'the assistant\'s earlier answer is in the history, as written')
        self.assertLess(users[-1], answers[-1], 'history oldest first')
        attachments = entries[users[-1]]['attachments']
        self.assertEqual(len(attachments), 1, 'the earlier message\'s file')
        att = attachments[0]
        self.assertEqual((att['name'], att['mimetype'], att['size']),
                         (history['attachment']['name'], history['attachment']['mimetype'],
                          len(base64.b64decode(history['attachment']['base64']))))
        self.assertTrue(att['url'].startswith(('https://', 'http://')), 'an absolute signed link')
        self.assertRegex(att['expires_at'], n8n['time_pattern'], 'expires_at is ISO 8601 UTC')
        seconds = (_parse_time(att['expires_at']) - datetime.now(timezone.utc)).total_seconds()
        low, high = history['expires_in']
        self.assertTrue(low <= seconds <= high, 'the link is valid one hour (%s s)' % seconds)

    def _contract_check_litellm_history(self, rules, spec, payload):
        history = rules['history']
        turns = payload['messages'][1:]
        texts = [_turn_text(turn) for turn in turns]
        users = [i for i, turn in enumerate(turns) if turn['role'] == 'user' and history['user_text'] in texts[i]]
        answers = [i for i, turn in enumerate(turns)
                   if turn['role'] == 'assistant' and history['assistant_text'] in texts[i]]
        self.assertTrue(users and answers, 'the earlier turn is in the messages')
        self.assertLess(users[-1], answers[-1])
        self.assertLess(answers[-1], len(turns) - 1, 'the question last')
        if not self.contract_text_only:
            content = turns[users[-1]]['content']
            images = [part for part in (content if isinstance(content, list) else [])
                      if isinstance(part, dict) and part.get('type') == 'image_url']
            self.assertEqual(len(images), 1, 'the earlier image goes inline')
            self.assertTrue(images[0]['image_url']['url'].startswith('data:image/png;base64,'), 'as a data URI')

    def _contract_check_litellm_request(self, rules, spec, url, kwargs, data):
        rule = rules['litellm']
        body = self._contract_body(kwargs, data, 'openai')
        headers = _lower_headers(kwargs)
        self.assertEqual(url, spec['endpoint'].rstrip('/') + rule['path_suffix'])
        self.assertTrue((headers.get('content-type') or '').startswith(rule['content_type']))
        self.assertEqual(headers.get('authorization'), rule['authorization'].format(key=spec['key']))
        self.assertIs(kwargs.get('allow_redirects'), False)
        self.assertNotIn(spec['person_email'].encode('utf-8'), body, 'never the email to LiteLLM')
        payload = json.loads(body.decode('utf-8'))
        for key in rule['required_keys']:
            self.assertIn(key, payload)
        for key in rule['forbidden_keys']:
            self.assertNotIn(key, payload)
        self.assertEqual(payload['model'], spec['model'])
        if spec['stream']:
            self.assertEqual(headers.get('accept'), rule['stream_accept'])
            for key, value in rule['stream_values'].items():
                self.assertEqual(payload.get(key), value, key)
        else:
            self.assertIn(payload.get('stream'), (None, False))
            self.assertNotIn('stream_options', payload)
        messages = payload['messages']
        self.assertEqual(messages[0]['role'], 'system')
        self.assertTrue(messages[0]['content'].startswith(spec['prompt']), messages[0]['content'][:80])
        turns = messages[1:]
        self.assertTrue(turns and turns[0]['role'] == 'user' and turns[-1]['role'] == 'user', 'user turns first and last')
        self.assertTrue(all(turn['role'] in ('user', 'assistant') for turn in turns))
        self.assertIn(self._contract_expected_text(spec), _turn_text(turns[-1]), 'the question, labelled as on n8n')
        self.assertTrue(self._contract_session_pattern().fullmatch(payload['user']), payload['user'])
        metadata = payload['metadata']
        meta = rule['metadata']
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()), 'strings only')
        self.assertLessEqual(len(metadata), meta['max_keys'])
        self.assertTrue(all(len(v) <= meta['max_value_length'] for v in metadata.values()))
        self.assertEqual(next(iter(metadata)), meta['first_key'])
        self.assertEqual(metadata[meta['first_key']], payload['user'])
        extra = meta['allowed_extra_keys'].get(self.contract_surface_type, [])
        for key in list(metadata)[1:]:
            self.assertTrue(key.startswith(tuple(meta['key_prefixes'])) or key in extra, key)
            self.assertNotIn(meta['forbidden_key_substring'], key.lower())
        self.assertEqual(metadata.get('surface_type'), self.contract_surface_type)
        if '@' in spec['person_name']:
            self.assertNotIn(spec['person_name'].encode('utf-8'), body, 'a name with an @ never reaches LiteLLM')
        return payload

    # --- 03: vectors ---

    def contract_check_03_vectors(self):
        rules = contract_fixture('requests')
        sig = rules['signature']
        body = sig['body'].encode('utf-8')
        self.assertEqual(json.dumps(json.loads(sig['body']), ensure_ascii=False, separators=(',', ':')), sig['body'])
        headers = self.contract_sign(sig['secret'], body, sig['timestamp'])
        self.assertEqual({name: headers.get(name) for name in sig['headers']}, sig['headers'])
        session = rules['session']
        self.assertEqual(self.contract_session_id(session['db_uuid'], session['channel_id']),
                         session['expected'][self.contract_session_prefix])

    # --- 04: render and the identity name ---

    def contract_check_04_render(self):
        render = contract_fixture('render')
        for case in render['links']:
            with self.subTest(case=case['id']):
                self.assertEqual(_hrefs(self.contract_render(case['text'], case['link_hosts'])), case['hrefs'],
                                 case['id'])
        if self.contract_link_policy == 'any_host':
            for case in render['any_host_links']:
                with self.subTest(case=case['id']):
                    self.assertEqual(_hrefs(self.contract_render(case['text'], None)), case['hrefs'], case['id'])
        for case in render['nesting']:
            if case['kind'] == 'quote':
                text = '>' * case['depth'] + ' ' + case['text']
            else:
                text = '\n'.join('%s- %s%d' % ('  ' * i, case['text'], i) for i in range(case['depth']))
            with self.subTest(case=case['id']):
                self.assertIn(case['must_contain'], str(self.contract_render(text, None)))
        if not self.contract_identity_block:
            return
        for case in render['identity_names']:
            with self.subTest(case=case['id']):
                self._contract_check_identity_name(self.contract_identity_text(case['raw']), case)

    def _contract_check_identity_name(self, text, case):
        raw, clean = case['raw'], case['clean']
        if clean:
            self.assertIn(clean, text, case['id'])
            start = contract_fixture('render')['identity_order']['account_line_start']
            lines = text.splitlines()
            account = next((i for i, line in enumerate(lines) if line.lstrip().startswith(start)), None)
            named = next((i for i, line in enumerate(lines) if clean in line), None)
            self.assertIsNotNone(account, 'an account line (%s)' % start)
            self.assertLess(account, named, 'the kind of account comes before the name (3.14)')
        else:
            self.assertNotIn('@', text, 'a name with an @ counts as no name: %s' % case['id'])
        if raw != clean:
            self.assertNotIn(raw, text, case['id'])
        if clean and raw.startswith(clean) and len(raw) > len(clean):
            self.assertNotIn(clean + raw[len(clean)], text, 'at most %s characters' % len(clean))
        for start in case.get('forbidden_line_starts', []):
            self.assertFalse(any(line.lstrip().startswith(start) for line in text.splitlines()), start)

    # --- 05: glossary ---

    def contract_check_05_glossary(self):
        import polib  # noqa: PLC0415 - Odoo 18 requires it
        glossary = contract_fixture('glossary')
        with file_open('%s/i18n/zh_TW.po' % self.contract_module) as fp:
            po = polib.pofile(fp.read())
        entries = {entry.msgid: entry.msgstr for entry in po if not entry.obsolete}
        for msgid, msgstr in entries.items():
            if msgid in glossary['msgids']:
                self.assertEqual(msgstr, glossary['msgids'][msgid], 'msgid %r' % msgid)
            for banned in glossary['banned_msgstr']:
                self.assertNotIn(banned, msgstr, 'msgstr of %r' % msgid[:60])
        for banned in glossary['banned_msgids']:
            self.assertNotIn(banned, entries)
        self.env['res.lang']._activate_lang('zh_TW')
        self.env['ir.module.module']._load_module_terms([self.contract_module], ['zh_TW'], overwrite=True)
        self.env.registry.clear_cache()
        self.env.invalidate_all()
        for kind, labels in glossary['fields'].items():
            model = self.env[self.contract_models[kind]]
            for name in glossary['required_fields'][kind]:
                self.assertIn(name, model._fields, '%s.%s' % (model._name, name))
            described = model.with_context(lang='zh_TW').fields_get(attributes=['string', 'selection'])
            for name, label in labels.items():
                if name in described:
                    self.assertEqual(described[name]['string'], label, '%s.%s' % (model._name, name))
            for name, keys in glossary['selection'].get(kind, {}).items():
                self.assertIn(name, described, '%s.%s' % (model._name, name))
                options = dict(described[name]['selection'])
                for key, label in keys.items():
                    self.assertEqual(options.get(key), label, '%s.%s %s' % (model._name, name, key))

    # --- 06: neutralize ---

    def contract_check_06_neutralize(self):
        with file_open('%s/data/neutralize.sql' % self.contract_module) as fp:
            sql = fp.read()
        for statement in _sql_statements(sql):
            self.assertRegex(statement, r'(?is)^DO\s+\$\$', 'every statement is a guarded DO block: %s' % statement[:60])
            targets = {table for table in re.findall(
                r'(?is)\b(?:UPDATE|DELETE\s+FROM|INSERT\s+INTO|TRUNCATE(?:\s+TABLE)?)\s+(?:ONLY\s+)?"?([a-z_][a-z0-9_]*)"?',
                statement) if table.lower() != 'set'}  # ON CONFLICT ... DO UPDATE SET names no table
            self.assertNotRegex(statement, r'(?i)\bEXECUTE\b',
                                'no dynamic SQL: every changed table must be visible to this check')
            for table in targets:
                guard = (r"(?is)to_regclass\(\s*'(?:public\.)?%s'\s*\)" % re.escape(table),
                         r"(?is)information_schema\.\w+.*?table_name\s*=\s*'%s'" % re.escape(table))
                self.assertTrue(any(re.search(pattern, statement) for pattern in guard),
                                'neutralize.sql changes %s without a guard on that table' % table)
        self.contract_neutralize_seed()
        self.env.flush_all()
        cr = self.env.cr
        connections = self.env[self.contract_models['connection']]._table
        assistants = self.env[self.contract_models['assistant']]._table
        cr.execute('SELECT count(*) FROM "%s"' % connections)  # noqa: S608 - a model's table
        self.assertTrue(cr.fetchone()[0], 'seeded connections')
        with cr.savepoint(flush=False):
            cr.execute(sql.strip())  # as odoo neutralize does
        self.env.invalidate_all()
        cr.execute("""SELECT count(*) FROM "%s" WHERE endpoint_url IS NOT NULL OR active
                           OR customer_safe IS DISTINCT FROM (backend_type = 'openai')""" % connections)  # noqa: S608
        self.assertEqual(cr.fetchone()[0], 0, 'connections: no URL, inactive, customer_safe reset')
        for table, columns in ((connections, ('api_key_secret',)), (assistants, ('push_key_hash', 'push_key_hint'))):
            for column in columns:
                cr.execute("""SELECT 1 FROM information_schema.columns
                               WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s""",
                           (table, column))
                if cr.fetchone():
                    cr.execute('SELECT count(*) FROM "%s" WHERE "%s" IS NOT NULL' % (table, column))  # noqa: S608
                    self.assertEqual(cr.fetchone()[0], 0, '%s.%s cleared' % (table, column))
        for label, query in self.contract_neutralize_extra():
            cr.execute(query)
            self.assertEqual(cr.fetchone()[0], 0, label)

    # --- 07: SHA256SUMS ---

    def contract_check_07_sums(self):
        with open(os.path.join(HERE, 'SHA256SUMS'), encoding='utf-8') as fp:
            lines = [line.rstrip('\n') for line in fp if line.strip()]
        listed = {}
        for line in lines:
            digest, name = line.split(None, 1)
            listed[name.lstrip('*')] = digest
        for name, digest in sorted(listed.items()):
            path = os.path.normpath(os.path.join(HERE, name))
            if name.startswith('../') and not os.path.exists(path):
                continue  # outside the module (docs/CONTRACT.md): checked in a repository checkout only
            with open(path, 'rb') as fp:
                self.assertEqual(hashlib.sha256(fp.read()).hexdigest(), digest, name)
        docs = os.path.normpath(os.path.join(HERE, '..', '..', '..', 'docs'))
        if os.path.isdir(docs):
            self.assertTrue(os.path.exists(os.path.join(docs, 'CONTRACT.md')),
                            'a repository checkout (docs/ exists) carries docs/CONTRACT.md')
        with open(os.path.join(HERE, 'SHA256SUMS'), 'rb') as fp:
            sums = hashlib.sha256(fp.read()).hexdigest()
        with open(os.path.join(HERE, 'READY'), encoding='utf-8') as fp:
            self.assertEqual(fp.read().strip(), sums, 'READY holds the SHA-256 of SHA256SUMS')
        present = {name for name in os.listdir(HERE) if name not in ('SHA256SUMS', 'READY', '__pycache__')}
        self.assertEqual(present, {name for name in listed if not name.startswith('../')},
                         'tests/contract holds exactly the files of the contract package')
        for name in FIXTURES:
            self.assertEqual(contract_fixture(name)['contract'], CONTRACT_VERSION, name)
        defined = {name: method for name, method in type(self).__dict__.items() if name.startswith('test_contract_')}
        for check in CHECKS:
            wrappers = [method for name, method in defined.items() if name.startswith('test_contract_%s' % check)]
            self.assertTrue(wrappers, 'test_contract_%s is defined by the product glue' % check)
            for method in wrappers:
                self.assertFalse(getattr(method, '__unittest_skip__', False), 'test_contract_%s is skipped' % check)
                self.assertIn('contract_check_%s(' % check, inspect.getsource(method),
                              'test_contract_%s calls contract_check_%s' % (check, check))
        values = contract_fixture('requests')['products'].get(self.contract_module)
        self.assertTrue(values, '%s is a product of the contract (3.1)' % self.contract_module)
        for flag in PRODUCT_FLAGS:
            self.assertEqual(getattr(self, 'contract_' + flag), values[flag], 'contract_%s (CONTRACT.md 3.1)' % flag)

    # --- 08: secrets ---

    def contract_check_08_secrets(self):
        # assertLogs lowers the root logger only: Odoo's --log-level sets the 'odoo' logger, which would drop
        # the INFO and DEBUG records before they are made
        loggers = [logging.getLogger('odoo'), logging.getLogger('odoo.addons.%s' % self.contract_module)]
        levels = [logger.level for logger in loggers]
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        try:
            self._contract_check_secrets()
        finally:
            for logger, level in zip(loggers, levels):
                logger.setLevel(level)

    def _contract_check_secrets(self):
        conv = contract_fixture('requests')['conversation']
        failures = {
            'http_401': FakeResponse(401, body={'error': {'message': 'invalid key %s' % SECRET_KEY,
                                                          'detail': SECRET_BODY}}),
            'timeout': requests.ReadTimeout('Read timed out (%s %s)' % (SECRET_KEY, SECRET_BODY)),
            'exception': RuntimeError('unexpected failure %s %s' % (SECRET_KEY, SECRET_BODY)),
            'header_error': requests.exceptions.InvalidHeader(
                "Invalid leading whitespace, reserved character(s), or return character(s) in header value: "
                "'Bearer %s'" % SECRET_KEY),
        }
        paths = [('litellm', {'backend': 'openai'})]
        if self.contract_streams:
            paths.append(('litellm streamed', {'backend': 'openai', 'stream': True}))
        # n8n: the signing secret, and a workflow URL that is itself a secret
        paths.append(('n8n', {'backend': 'n8n', 'endpoint': conv['endpoint_n8n'] + '/' + SECRET_KEY}))
        cases = [(path, label, failure, values) for path, values in paths for label, failure in failures.items()]
        if self.contract_streams:
            stream_error = 'data: %s\n\n' % json.dumps({'error': {'message': 'quota %s' % SECRET_KEY,
                                                                   'detail': SECRET_BODY}})
            cases.append(('litellm streamed', 'error_chunk', FakeResponse(200, chunks=[stream_error]),
                          {'backend': 'openai', 'stream': True}))
        for path, label, failure, values in cases:
            spec = self.contract_spec(key=SECRET_KEY, log_payload=True, **values)
            kwargs = {'return_value': failure} if isinstance(failure, FakeResponse) else {'side_effect': failure}
            with self.subTest(path=path, failure=label):
                with self.assertLogs(level='DEBUG') as logs, self._contract_patch_post(**kwargs), \
                        self._contract_patch_sleep():
                    _logger.info('contract check 08: %s %s', path, label)
                    artifacts = self.contract_turn(spec)
                # every log line (and its traceback) at any level, except Odoo's own SQL query log (assertLogs
                # turns it on: it shows the parameters of every query, the stored key included)
                formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
                texts = [formatter.format(record) for record in logs.records
                         if not record.name.startswith('odoo.sql_db')]
                for key in ('call_logs', 'job_errors', 'messages'):
                    texts += [str(text) for text in artifacts.get(key) or [] if text]
                self.assertTrue(artifacts.get('call_logs'), 'a call log is written')
                for text in texts:
                    self.assertNotIn(SECRET_KEY, text, '%s %s' % (path, label))
                    self.assertNotIn(SECRET_BODY, text, '%s %s' % (path, label))

    # --- 09: customer_safe ---

    def contract_check_09_customer_safe(self):
        from odoo.tests import Form  # noqa: PLC0415
        conv = contract_fixture('requests')['conversation']
        Connection = self.env[self.contract_models['connection']]
        litellm = Connection.create({'name': 'Contract LiteLLM', 'backend_type': 'openai',
                                     'endpoint_url': conv['endpoint_litellm'], 'customer_safe': False})
        self.assertTrue(litellm.customer_safe, 'LiteLLM: always safe for customers (create)')
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            litellm.write({'customer_safe': False})
            litellm.flush_recordset()
        litellm.invalidate_recordset()
        self.assertTrue(litellm.customer_safe)
        self.assertTrue(litellm.copy().customer_safe, 'LiteLLM: copy')
        n8n = Connection.create({'name': 'Contract n8n', 'backend_type': 'n8n', 'endpoint_url': conv['endpoint_n8n']})
        self.assertFalse(n8n.customer_safe, 'n8n: not safe until an administrator ticks it')
        n8n.customer_safe = True
        self.assertTrue(n8n.customer_safe)
        n8n.endpoint_url = conv['endpoint_n8n'] + '-other'
        self.assertFalse(n8n.customer_safe, 'n8n: cleared when the URL changes')
        n8n.customer_safe = True
        n8n.backend_type = 'openai'
        self.assertTrue(n8n.customer_safe, 'LiteLLM: write')
        n8n.backend_type = 'n8n'
        self.assertFalse(n8n.customer_safe, 'n8n: cleared when the type changes')
        n8n.write({'backend_type': 'openai', 'customer_safe': False})
        self.assertTrue(n8n.customer_safe, 'LiteLLM: write, even with customer_safe False')
        with Form(Connection) as form:
            form.name = 'Contract form'
            form.endpoint_url = conv['endpoint_litellm']
            form.backend_type = 'n8n'
            self.assertFalse(form.customer_safe, 'onchange: n8n')
            form.backend_type = 'openai'
            self.assertTrue(form.customer_safe, 'onchange: LiteLLM')
        self.assertTrue(form.record.customer_safe)
        safe = Connection.create({'name': 'Contract safe n8n', 'backend_type': 'n8n',
                                  'endpoint_url': conv['endpoint_n8n']})
        safe.customer_safe = True
        with Form(safe) as form:
            form.endpoint_url = conv['endpoint_n8n'] + '-changed'
            self.assertFalse(form.customer_safe, 'onchange: n8n URL changed')

    # --- 10: retries and deadline ---

    def contract_check_10_retries(self):
        for backend in ('openai', 'n8n'):
            with self.subTest(backend=backend):
                self._contract_check_retries(backend)

    def _contract_check_retries(self, backend):
        def run(effects, timeout=300, retries=2):
            spec = self.contract_spec(backend=backend, stream=False, timeout=timeout, retries=retries)
            with self._contract_patch_post(side_effect=list(effects)) as post, \
                    patch(self.contract_sleep_target) as sleep:
                result = self.contract_call(spec)
            for call in post.call_args_list:
                self.assertIs(call.kwargs.get('allow_redirects'), False, 'redirects are never followed')
            waits = [call.args[0] if call.args else call.kwargs.get('secs') for call in sleep.call_args_list]
            return result.get('status'), post.call_count, waits

        answer = contract_fixture('requests')['answers']['litellm' if backend == 'openai' else 'n8n']

        def ok():
            return FakeResponse(200, body=answer)

        def status(code, **headers):
            return FakeResponse(code, body={'error': {'message': 'HTTP %s' % code}}, headers=headers)

        cases = [
            ('429 then 503, retried after 3 and 10 s', [status(429), status(503), ok()], {}, ('ok', 3, [3, 10])),
            ('429 three times', [status(429), status(429), status(429)], {}, ('http_error', 3, [3, 10])),
            ('no retries', [status(503), ok()], {'retries': 0}, ('http_error', 1, [])),
            ('not sent: refused', [_refused(), ok()], {'retries': 1}, ('ok', 2, [3])),
            ('not sent: connect timeout', [requests.ConnectTimeout('connect timeout'), ok()], {'retries': 1},
             ('ok', 2, [3])),
            ('read timeout', [requests.ReadTimeout('read timeout'), ok()], {}, ('timeout', 1, [])),
            ('401', [status(401), ok()], {}, ('http_error', 1, [])),
            ('reset after sending', [_reset_after_send(), ok()], {}, ('error', 1, [])),
            ('502', [status(502), ok()], {}, ('http_error', 1, [])),
            ('504', [status(504), ok()], {}, ('http_error', 1, [])),
            ('redirect', [status(302, Location='https://elsewhere.example/v1/chat/completions'), ok()], {},
             ('http_error', 1, [])),
            ('deadline: no retry past it', [status(429), status(429), status(429)], {'timeout': 10},
             ('http_error', 2, [3])),
        ]
        for label, effects, options, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(run(effects, **options), expected, '%s: %s' % (backend, label))

    # --- 11: gate ---

    def contract_check_11_gate(self):
        with self._contract_patch_post() as post:
            handle = self.contract_gate_start()
            self.contract_gate_block(handle, True)
            self.contract_gate_post(handle, '第一則')
            self.contract_gate_post(handle, '第二則')
            state = self.contract_gate_state(handle)
            self.assertEqual(state['jobs'], 0, 'blocked: no job')
            self.assertEqual(state['notices'], ['notification'], 'blocked: one notification per blocked period')
            self.contract_gate_block(handle, False)
            self.contract_gate_post(handle, '第三則')
            state = self.contract_gate_state(handle)
            self.assertEqual(state['jobs'], 1, 'answering again: a job')
            self.assertEqual(len(state['notices']), 1)
            self.contract_gate_block(handle, True)
            self.contract_gate_post(handle, '第四則')
            state = self.contract_gate_state(handle)
            self.assertEqual(state['jobs'], 1)
            self.assertEqual(state['notices'], ['notification', 'notification'], 'a new blocked period: a new notice')
        self.assertFalse(post.called, 'no backend call while blocked')
        # 3.5 step 2, checked again when the job runs: queued while the AI could answer, blocked before it runs
        with self._contract_patch_post() as post:
            handle = self.contract_gate_start()
            self.contract_gate_post(handle, '排隊中')
            self.assertEqual(self.contract_gate_state(handle)['jobs'], 1, 'queued')
            self.contract_gate_block(handle, True)
            self.contract_gate_run(handle)
            self.assertEqual(self.contract_gate_state(handle)['notices'], ['notification'],
                             'blocked when the job runs: one notice')
        self.assertFalse(post.called, 'no backend call for a job blocked when it runs')

    # --- 13: connections ---

    def contract_check_13_connections(self):
        conv = contract_fixture('requests')['conversation']
        Connection = self.env[self.contract_models['connection']]
        flow = Connection.create({'name': 'Contract n8n default', 'backend_type': 'n8n',
                                  'endpoint_url': conv['endpoint_n8n']})
        self.assertEqual(flow.timeout_seconds, contract_fixture('requests')['n8n']['default_timeout'],
                         'a new n8n connection waits the n8n default (3.3)')
        litellm = Connection.create({'name': 'Contract key', 'backend_type': 'openai', 'model': conv['model'],
                                     'endpoint_url': conv['endpoint_litellm'], 'api_key': SECRET_KEY})
        self.assertTrue(litellm.api_key_set)
        litellm.invalidate_recordset()
        self.assertNotIn(SECRET_KEY, json.dumps(litellm.read(['api_key', 'api_key_set']), default=str),
                         'the key is write-only: read')
        self.assertNotIn(SECRET_KEY, json.dumps(litellm.export_data(['name', 'api_key']), default=str),
                         'the key is write-only: export')
        user = new_test_user(self.env, login='contract_not_admin', groups='base.group_user')
        with self.assertRaises(AccessError, msg='only system administrators read connections (3.2)'):
            Connection.with_user(user).search([], limit=1)

    # --- 12: identity block ---

    def contract_check_12_identity_block(self):
        rules = contract_fixture('requests')
        for case in contract_fixture('render')['identity_names']:
            spec = self.contract_spec(backend='openai', stream=False, person_name=case['raw'])
            with self.subTest(case=case['id']):
                _url, kwargs, data = self._contract_turn_capture(spec, FakeResponse(200, body=rules['answers']['litellm']))
                body = self._contract_body(kwargs, data, 'openai')
                self.assertNotIn(spec['person_email'].encode('utf-8'), body)
                payload = json.loads(body.decode('utf-8'))
                first = payload['messages'][0]
                system = first['content'] if first['role'] == 'system' else ''
                if self.contract_identity_block:
                    self._contract_check_identity_name(system, case)
                else:
                    if case['clean']:
                        self.assertNotIn(case['clean'], system, 'no identity block')
                    self.assertNotIn(case['raw'], system, 'no identity block')
