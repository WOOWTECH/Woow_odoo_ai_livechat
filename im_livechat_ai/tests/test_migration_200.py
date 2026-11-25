# -*- coding: utf-8 -*-
"""18.0.1.x -> 18.0.2.0.0: the migration (models/ai_migration.py and
migrations/18.0.2.0.0), the upgrade guards and neutralize.sql.

The 18.0.1.x channel columns are added in the test transaction and seeded with
SQL, as an upgraded database has them; then _migrate_legacy_channels() runs.
All HTTP is mocked; no real key."""

import hashlib
import hmac
import importlib.util
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase
from odoo.tools import file_open, mute_logger

from .common import add_legacy_channel_columns, livechat_operator, make_ai_channel, run_ai_job, \
    set_legacy_channel_values, TEST_BACKEND

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
SENTINEL = 'sk-sentinel-migration-key-0123456789'
MIGRATION_LOGGER = 'odoo.addons.im_livechat_ai.models.ai_migration'


def load_migration(version, name):
    path = f"{get_module_path('im_livechat_ai')}/migrations/{version}/{name}.py"
    spec = importlib.util.spec_from_file_location(
        f'im_livechat_ai_mig200_{version.replace(".", "_")}_{name.replace("-", "_")}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ok(text='好的'):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {'choices': [{'message': {'content': text}}], 'usage': {}}
    return response


class MigrationCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_legacy_channel_columns(cls.env.cr)
        cls.Channel = cls.env['im_livechat.channel']
        cls.Connection = cls.env['im_livechat.ai.connection'].with_context(active_test=False)
        cls.Assistant = cls.env['im_livechat.ai.assistant'].with_context(active_test=False)
        cls.category = cls.env.ref('im_livechat_ai.partner_category_ai_bot')

    def legacy(self, name, enabled=True, token=None, **values):
        """A channel as 18.0.1.x left it (SQL)."""
        channel = self.Channel.create({'name': name})
        self.env.flush_all()
        values.setdefault('ai_backend_type', 'openai')
        if token is not None:
            values['ai_api_token_secret'] = token
        set_legacy_channel_values(self.env.cr, channel.id, **values)
        self.env.cr.execute("UPDATE im_livechat_channel SET ai_enabled = %s WHERE id = %s", (enabled, channel.id))
        self.env.invalidate_all()
        return channel

    def migrate(self):
        with mute_logger(MIGRATION_LOGGER):
            stats = self.Connection._migrate_legacy_channels()
        self.env.invalidate_all()
        return stats

    def bot(self, name='舊機器人', image=True):
        vals = {'name': name, 'active': False, 'category_id': [(4, self.category.id)]}
        if image:
            vals['image_1920'] = self.Channel._ai_bot_avatar()
        return self.env['res.partner'].create(vals)

    def key_equals(self, connection, value):
        self.env.flush_all()
        self.env.cr.execute("SELECT api_key_secret IS NOT DISTINCT FROM %s FROM im_livechat_ai_connection "
                            "WHERE id = %s", (value, connection.id))
        return self.env.cr.fetchone()[0]


class TestMigration200(MigrationCase):

    def test_litellm_with_key(self):
        bot = self.bot('官網機器人')
        channel = self.legacy('LiteLLM', ai_endpoint_url='https://litellm.example/v1/chat/completions/',
                              ai_model=' pool/m ', ai_system_prompt='提示', ai_max_history=30, ai_max_retries=2,
                              ai_error_message='錯誤', ai_bot_partner_id=bot.id, ai_bot_name=' 小沃 ',
                              token='dummy-key-a')
        stats = self.migrate()
        self.assertEqual(stats['channels'], [channel.id])
        assistant = channel.ai_assistant_id
        connection = assistant.connection_id
        self.assertTrue(channel.ai_enabled, 'ai_enabled is untouched')
        self.assertEqual((connection.backend_type, connection.endpoint_url, connection.model),
                         ('openai', 'https://litellm.example/v1', 'pool/m'))
        self.assertEqual((connection.timeout_seconds, connection.max_retries, connection.document_mode),
                         (45, 2, 'file'))
        self.assertTrue(self.key_equals(connection, 'dummy-key-a'))
        self.assertTrue(connection.legacy_migrated)
        self.assertTrue(connection.customer_safe)
        self.assertFalse(connection.log_payload)
        self.assertEqual(connection.readiness, 'ok')
        self.assertEqual((assistant.name, assistant.system_prompt, assistant.max_history, assistant.error_message),
                         ('小沃', '提示', 30, '錯誤'))
        self.assertEqual(assistant.partner_id, bot)
        self.assertTrue(assistant.livechat_available)
        self.assertTrue(assistant.image_1920)
        self.assertIn(str(channel.id), assistant.description)
        self.assertIn(str(channel.id), connection.description)
        self.assertEqual(channel.ai_state, 'ok')

    def test_n8n_without_and_with_key(self):
        without = self.legacy('n8n no key', ai_backend_type='n8n', ai_endpoint_url='https://n8n.example/hook/a',
                              ai_request_timeout=0, ai_model='ignored')
        signed = self.legacy('n8n key', ai_backend_type='n8n', ai_endpoint_url='https://n8n.example/hook/b',
                             ai_request_timeout=200, token='dummy-signing-secret')
        self.migrate()
        first = without.ai_assistant_id.connection_id
        self.assertEqual((first.backend_type, first.timeout_seconds, first.model), ('n8n', 120, False))
        self.assertFalse(first.api_key_set)
        self.assertFalse(first.customer_safe, 'F4: n8n waits for an administrator')
        self.assertEqual(first.readiness, 'ok')
        second = signed.ai_assistant_id.connection_id
        self.assertEqual(second.timeout_seconds, 200)
        self.assertTrue(self.key_equals(second, 'dummy-signing-secret'))
        # the call is signed with the original secret
        with patch(POST) as mock_post:
            mock_post.return_value = MagicMock(status_code=200, json=MagicMock(return_value={'reply': 'ok'}))
            session = self.env['discuss.channel'].create({
                'name': 'S', 'channel_type': 'livechat', 'livechat_channel_id': signed.id,
                'livechat_operator_id': livechat_operator(self.env).id})
            self.env['mail.message'].create({'body': 'hi', 'model': 'discuss.channel', 'res_id': session.id,
                                             'message_type': 'comment', 'author_id': False})
            signed._generate_ai_reply(session, session._ai_conversation(signed))
            headers, body = mock_post.call_args.kwargs['headers'], mock_post.call_args.kwargs['data']
        expected = 'sha256=' + hmac.new(b'dummy-signing-secret', headers['X-Woow-Timestamp'].encode() + b'.' + body,
                                        hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Woow-Signature'], expected)

    def test_dedupe_by_settings_and_key(self):
        common = {'ai_endpoint_url': 'https://litellm.example/v1', 'ai_model': 'm'}
        one = self.legacy('One', token='dummy-key-b', **common)
        two = self.legacy('Two', token='dummy-key-b\n', **common)  # the same key once stripped
        other_key = self.legacy('Three', token='dummy-key-c', **common)
        other_model = self.legacy('Four', token='dummy-key-b', ai_endpoint_url='https://litellm.example/v1',
                                  ai_model='m2')
        self.migrate()
        self.assertEqual(one.ai_assistant_id.connection_id, two.ai_assistant_id.connection_id)
        self.assertNotEqual(one.ai_assistant_id, two.ai_assistant_id, 'one assistant per channel')
        self.assertNotEqual(one.ai_assistant_id.connection_id, other_key.ai_assistant_id.connection_id)
        self.assertNotEqual(one.ai_assistant_id.connection_id, other_model.ai_assistant_id.connection_id)
        self.assertTrue(self.key_equals(one.ai_assistant_id.connection_id, 'dummy-key-b'))
        with patch(POST) as mock_post:
            mock_post.return_value = _ok()
            session = self.env['discuss.channel'].create({
                'name': 'S', 'channel_type': 'livechat', 'livechat_channel_id': two.id,
                'livechat_operator_id': livechat_operator(self.env).id})
            self.env['mail.message'].create({'body': 'hi', 'model': 'discuss.channel', 'res_id': session.id,
                                             'message_type': 'comment', 'author_id': False})
            two._generate_ai_reply(session, session._ai_conversation(two))
        self.assertEqual(mock_post.call_args.kwargs['headers']['Authorization'], 'Bearer dummy-key-b')

    def test_runtime_rules(self):
        channel = self.legacy('Rules', ai_endpoint_url='https://litellm.example/v1', ai_model='m', token='dummy',
                              ai_request_timeout=5, ai_max_retries=None, ai_max_history=0, ai_document_mode='bogus',
                              ai_backend_type='hermes')
        self.migrate()
        connection = channel.ai_assistant_id.connection_id
        self.assertEqual((connection.backend_type, connection.timeout_seconds, connection.max_retries,
                          connection.document_mode), ('openai', 10, 0, 'file'))
        self.assertEqual(channel.ai_assistant_id.max_history, 50)
        clamp = self.legacy('Clamp', ai_endpoint_url='https://litellm.example/v1', ai_model='m', token='dummy',
                            ai_request_timeout=900, ai_max_retries=7, ai_max_history=999)
        self.migrate()
        connection = clamp.ai_assistant_id.connection_id
        self.assertEqual((connection.timeout_seconds, connection.max_retries), (300, 2))
        self.assertEqual(clamp.ai_assistant_id.max_history, 200)

    def test_bot_partner_reuse_rules(self):
        reused = self.bot('可用')
        person = self.env['res.users'].create({'name': 'Person', 'login': 'mig_person'}).partner_id
        default_bot = self.env.ref('im_livechat_ai.partner_ai_bot')
        a = self.legacy('A', ai_bot_partner_id=reused.id, ai_endpoint_url='https://x.example/v1')
        b = self.legacy('B', ai_bot_partner_id=person.id, ai_endpoint_url='https://x.example/v1')
        c = self.legacy('C', ai_bot_partner_id=default_bot.id, ai_endpoint_url='https://x.example/v1')
        d = self.legacy('D', ai_bot_partner_id=reused.id, ai_endpoint_url='https://x.example/v1')
        stats = self.migrate()
        self.assertEqual(a.ai_assistant_id.partner_id, reused)
        for channel in (b, c, d):
            self.assertNotIn(channel.ai_assistant_id.partner_id, person | default_bot | reused)
            self.assertFalse(channel.ai_assistant_id.partner_id.active)
            self.assertIn(self.category, channel.ai_assistant_id.partner_id.category_id)
        self.assertEqual((stats['partners_reused'], stats['partners_created']), (1, 3))
        self.assertNotIn(self.category, person.category_id, 'a person is never tagged')

    def test_untagged_bot_gets_the_tag(self):
        bot = self.env['res.partner'].create({'name': 'untagged', 'active': False})
        self.legacy('Off', enabled=False, ai_bot_partner_id=bot.id)  # AI off, no URL: not migrated
        self.migrate()
        self.assertIn(self.category, bot.category_id)

    def test_content_erased_and_logs_linked(self):
        channel = self.legacy('Logged', ai_endpoint_url='https://x.example/v1', ai_model='m', token='dummy',
                              ai_log_payload=True)
        quiet = self.legacy('Quiet', ai_endpoint_url='https://y.example/v1', ai_model='m', token='dummy')
        Log = self.env['llm.api.log']
        logged = Log.create({'status': 'success', 'livechat_channel_id': channel.id,
                             'request_payload': '{"private": 1}', 'response_payload': '{"x": 2}'})
        kept = Log.create({'status': 'success', 'livechat_channel_id': quiet.id, 'request_payload': '{"y": 1}'})
        stats = self.migrate()
        self.assertFalse(logged.request_payload)
        self.assertFalse(logged.response_payload)
        self.assertEqual(kept.request_payload, '{"y": 1}', "only the channels that logged content")
        self.assertEqual(stats['logs_erased'], 1)
        self.assertEqual(logged.assistant_id, channel.ai_assistant_id)
        self.assertEqual(logged.connection_id, channel.ai_assistant_id.connection_id)
        self.assertFalse(channel.ai_assistant_id.connection_id.log_payload)

    def test_selection_and_blocked(self):
        nothing = self.legacy('Nothing', enabled=False)
        prompt_only = self.legacy('Prompt', enabled=False, ai_system_prompt='x')
        on_without_url = self.legacy('On no URL', enabled=True)
        stats = self.migrate()
        self.assertFalse(nothing.ai_assistant_id)
        self.assertTrue(prompt_only.ai_assistant_id)
        self.assertTrue(on_without_url.ai_assistant_id)
        self.assertEqual(stats['blocked'], [on_without_url.id])
        self.assertTrue(on_without_url.ai_enabled)
        self.assertEqual(on_without_url.ai_state, 'blocked')

    @patch(POST)
    def test_pause_and_jobs_are_kept_and_answered(self, mock_post):
        mock_post.return_value = _ok('補答')
        channel = self.legacy('Paused', ai_endpoint_url='https://x.example/v1', ai_model='m', token='dummy-p')
        session = self.env['discuss.channel'].create({
            'name': 'S', 'channel_type': 'livechat', 'livechat_channel_id': channel.id,
            'livechat_operator_id': livechat_operator(self.env).id})
        message = self.env['mail.message'].create({'body': '在嗎', 'model': 'discuss.channel', 'res_id': session.id,
                                                   'message_type': 'comment', 'author_id': False})
        job = self.env['im_livechat.ai.job'].create({'discuss_channel_id': session.id, 'message_id': message.id,
                                                     'livechat_channel_id': channel.id})
        self.migrate()
        self.assertEqual(job.state, 'pending')
        job._run_job(commit=False)
        self.assertEqual(job.state, 'done')
        self.assertIn('補答', job.reply_message_id.body)

    def test_second_run_changes_nothing(self):
        self.legacy('A', ai_endpoint_url='https://x.example/v1', ai_model='m', token='dummy')
        self.legacy('B', ai_backend_type='n8n', ai_endpoint_url='https://n8n.example/h')
        first = self.migrate()
        counts = (self.Connection.search_count([]), self.Assistant.search_count([]))
        second = self.migrate()
        self.assertEqual((self.Connection.search_count([]), self.Assistant.search_count([])), counts)
        self.assertEqual(second['channels'], [])
        self.assertEqual(second['connections_created'], [])
        self.assertEqual(len(first['channels']), 2)
        # a channel added later (e.g. a rollback window) reuses the migrated connection
        later = self.legacy('C', ai_endpoint_url='https://x.example/v1', ai_model='m', token='dummy')
        third = self.migrate()
        self.assertEqual(third['connections_reused'], 1)
        self.assertEqual(later.ai_assistant_id.connection_id.legacy_migrated, True)

    def test_sentinel_key_never_logged(self):
        channel = self.legacy('Sentinel', ai_endpoint_url='https://x.example/v1', ai_model='m', token=SENTINEL)
        self.legacy('Sentinel twin', ai_endpoint_url='https://x.example/v1', ai_model='m', token=SENTINEL)
        with self.assertLogs(level=logging.DEBUG) as logs:
            stats = self.Connection._migrate_legacy_channels()
            post = load_migration('18.0.2.0.0', 'post-migrate')
            post.migrate(self.env.cr, '18.0.1.3.5')
        self.env.invalidate_all()
        dump = '\n'.join(logs.output) + json.dumps(stats, default=str)
        self.assertNotIn(SENTINEL, dump, 'not even in the SQL debug log: the key never enters a query text')
        own = '\n'.join(line for line in logs.output if 'im_livechat_ai' in line.split(':', 2)[1])
        self.assertNotIn('x.example', own, "no URL in this module's log lines either")
        assistant = channel.ai_assistant_id
        connection = assistant.connection_id
        for text in (connection.description, assistant.description, connection.name, assistant.name,
                     connection.readiness_reason, channel.ai_state_reason or ''):
            self.assertNotIn(SENTINEL, text or '')
        notes = self.env['mail.message'].search([('model', 'in', ('im_livechat.ai.connection',
                                                                  'im_livechat.ai.assistant'))])
        self.assertNotIn(SENTINEL, json.dumps(notes.mapped('body'), default=str))

    def test_hermes_lines(self):
        post = load_migration('18.0.2.0.0', 'post-migrate')
        self.legacy('Hermes host', ai_endpoint_url='https://hermes.example/v1', ai_model='x', token='d')
        self.legacy('Hermes model', ai_endpoint_url='https://other.example/v1', ai_model='hermes-agent', token='d')
        self.legacy('No model', ai_endpoint_url='https://third.example/v1', token='d')
        self.migrate()
        self.env.flush_all()
        found = post.hermes_connections(self.env.cr)
        possible = post.possibly_hermes_connections(self.env.cr)
        self.assertEqual(len(found), 2)
        self.assertTrue(set(possible) - set(found))


class TestUpgradeGuards(MigrationCase):
    """migrations/18.0.2.0.0/pre-migrate.py"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pre = load_migration('18.0.2.0.0', 'pre-migrate')

    def test_refuses_below_1_1_0(self):
        for version in ('18.0.1.0.0', '18.0.1.0.1'):
            with self.assertRaises(UserError) as caught:
                self.pre.migrate(self.env.cr, version)
            self.assertIn('18.0.1.3.5', str(caught.exception))
        with mute_logger(self.pre.__name__):
            for version in ('18.0.1.1.0', '18.0.1.2.1', '18.0.1.3.5'):
                self.pre.migrate(self.env.cr, version)
        self.pre.migrate(self.env.cr, None)  # fresh install

    def test_refuses_while_the_bridge_is_installed(self):
        self.env.cr.execute("""
            INSERT INTO ir_module_module (name, state, latest_version, create_uid, write_uid, create_date, write_date)
            VALUES ('woow_ai_contact_livechat', 'installed', '18.0.1.2.0', 1, 1, now(), now())
            ON CONFLICT (name) DO UPDATE SET state = 'installed'""")
        with self.assertRaises(UserError) as caught:
            self.pre.migrate(self.env.cr, '18.0.1.3.5')
        self.assertIn('woow_ai_contact_livechat', str(caught.exception))
        self.env.cr.execute("UPDATE ir_module_module SET state = 'uninstalled' WHERE name = 'woow_ai_contact_livechat'")
        with mute_logger(self.pre.__name__):
            self.pre.migrate(self.env.cr, '18.0.1.3.5')

    def test_restores_bot_xmlids_the_bridge_moved(self):
        cr = self.env.cr
        for module, prefix in (('woow_ai_contact_livechat', 'legacy_'),
                               ('__export__', 'woow_ai_contact_livechat_legacy_')):
            with self.subTest(module=module), self.env.cr.savepoint(flush=False):
                cr.execute("UPDATE ir_model_data SET module = %s, name = %s || name "
                           "WHERE module = 'im_livechat_ai' AND name IN ('partner_ai_bot', 'partner_category_ai_bot')",
                           (module, prefix))
                with mute_logger(self.pre.__name__):
                    self.pre.migrate(cr, '18.0.1.3.5')
                cr.execute("SELECT name, noupdate FROM ir_model_data WHERE module = 'im_livechat_ai' "
                           "AND name IN ('partner_ai_bot', 'partner_category_ai_bot') ORDER BY name")
                self.assertEqual(cr.fetchall(), [('partner_ai_bot', True), ('partner_category_ai_bot', True)])
                cr.execute("SELECT count(*) FROM ir_model_data WHERE module = %s AND name LIKE %s",
                           (module, prefix + 'partner_%'))
                self.assertEqual(cr.fetchone()[0], 0)

    def test_keeps_moved_column_reflections(self):
        cr = self.env.cr
        # the 18.0.1.x reflections as an upgraded database has them
        model_id = self.env['ir.model']._get_id('im_livechat.channel')
        names = []
        for field in self.pre.KEPT_FIELDS:
            cr.execute("""INSERT INTO ir_model_fields (model_id, model, name, field_description, ttype, state,
                                                       store, create_uid, write_uid, create_date, write_date)
                          VALUES (%s, 'im_livechat.channel', %s, '{"en_US": "x"}', 'char', 'base', TRUE, 1, 1,
                                  now(), now()) RETURNING id""", (model_id, field))
            field_id = cr.fetchone()[0]
            name = 'field_im_livechat_channel__%s' % field
            cr.execute("INSERT INTO ir_model_data (module, name, model, res_id, noupdate) "
                       "VALUES ('im_livechat_ai', %s, 'ir.model.fields', %s, FALSE)", (name, field_id))
            names.append(name)
        with mute_logger(self.pre.__name__):
            self.pre.migrate(cr, '18.0.1.3.5')
        cr.execute("SELECT count(*) FROM ir_model_data WHERE module = 'im_livechat_ai' AND name IN %s AND noupdate",
                   (tuple(names),))
        self.assertEqual(cr.fetchone()[0], len(self.pre.KEPT_FIELDS))

    def _retired_view(self, arch=None):
        """The retired 18.0.1.1.0 Settings view as an 18.0.1.1.0 database stored it."""
        if arch is None:
            with file_open('im_livechat_ai/tests/fixtures/legacy-settings-arch-18.0.1.1.0.xml') as fixture:
                arch = fixture.read().strip()
        base = self.env.ref('base.res_config_settings_view_form')
        self.env.cr.execute("""
            INSERT INTO ir_ui_view (name, model, type, inherit_id, mode, active, priority, arch_db, create_uid,
                                    write_uid, create_date, write_date)
            VALUES ('res.config.settings.view.form.inherit.im_livechat_ai', 'res.config.settings', 'form', %s,
                    'extension', TRUE, 16, %s, 1, 1, now(), now()) RETURNING id""",
                            (base.id, json.dumps({'en_US': arch})))
        view_id = self.env.cr.fetchone()[0]
        self.env.cr.execute("INSERT INTO ir_model_data (module, name, model, res_id, noupdate) VALUES "
                            "('im_livechat_ai', 'res_config_settings_view_form_livechat_ai', 'ir.ui.view', %s, FALSE)",
                            (view_id,))
        return view_id

    def test_retired_settings_view_is_removed_when_plain(self):
        """Draft PR #6 folded in: the exact 18.0.1.1.0 view is removed before the module loads."""
        view_id = self._retired_view()
        with mute_logger(self.pre.__name__):
            self.assertEqual(self.pre.remove_retired_settings_view(self.env.cr), view_id)
        self.env.cr.execute("SELECT count(*) FROM ir_ui_view WHERE id = %s", (view_id,))
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.assertFalse(self.env.ref('im_livechat_ai.res_config_settings_view_form_livechat_ai',
                                      raise_if_not_found=False))
        self.assertIsNone(self.pre.remove_retired_settings_view(self.env.cr), 'nothing left: a no-op')

    def test_retired_settings_view_changed_or_with_child_stops(self):
        view_id = self._retired_view()
        self.env.cr.execute("""
            INSERT INTO ir_ui_view (name, model, type, inherit_id, mode, active, priority, arch_db, create_uid,
                                    write_uid, create_date, write_date)
            VALUES ('child', 'res.config.settings', 'form', %s, 'extension', TRUE, 16, %s, 1, 1, now(), now())
            RETURNING id""", (view_id, json.dumps({'en_US': '<xpath expr="//form" position="inside"><div/></xpath>'})))
        child_id = self.env.cr.fetchone()[0]
        with self.assertRaises(UserError) as caught:
            self.pre.remove_retired_settings_view(self.env.cr)
        self.assertIn('not removed automatically', str(caught.exception))
        self.env.cr.execute("DELETE FROM ir_ui_view WHERE id = %s", (child_id,))
        self.env.cr.execute("UPDATE ir_ui_view SET arch_updated = TRUE WHERE id = %s", (view_id,))
        with self.assertRaises(UserError):
            self.pre.remove_retired_settings_view(self.env.cr)
        self.env.cr.execute("UPDATE ir_ui_view SET arch_updated = FALSE, arch_db = %s WHERE id = %s",
                            (json.dumps({'en_US': '<xpath expr="//form" position="inside"><div/></xpath>'}), view_id))
        with self.assertRaises(UserError):
            self.pre.remove_retired_settings_view(self.env.cr)

    def test_residual_settings_reference_stops(self):
        self.env.cr.execute("""
            INSERT INTO ir_ui_view (name, model, type, mode, active, priority, arch_db, create_uid, write_uid,
                                    create_date, write_date)
            VALUES ('custom settings', 'res.config.settings', 'form', 'primary', TRUE, 16, %s, 1, 1, now(), now())
        """, (json.dumps({'en_US': '<form><field name="livechat_ai_base_url"/></form>'}),))
        with self.assertRaises(UserError) as caught:
            self.pre.remove_retired_settings_view(self.env.cr)
        self.assertIn('livechat_ai_', str(caught.exception))


class TestNeutralize(TransactionCase):

    def _neutralize(self):
        with file_open('im_livechat_ai/data/neutralize.sql') as sql_file:
            self.env.flush_all()
            self.env.cr.execute(sql_file.read())
        self.env.invalidate_all()

    def test_upgraded_database(self):
        add_legacy_channel_columns(self.env.cr)
        channel = make_ai_channel(self.env, {**TEST_BACKEND, 'name': 'Prod', 'ai_enabled': True})
        n8n = make_ai_channel(self.env, {'name': 'n8n', 'ai_backend_type': 'n8n',
                                         'ai_endpoint_url': 'https://n8n.example/h', 'ai_api_token': 'dummy'})
        n8n.sudo().ai_assistant_id.connection_id.customer_safe = True
        self.env.flush_all()
        set_legacy_channel_values(self.env.cr, channel.id, ai_api_token_secret='dummy-legacy',
                                  ai_endpoint_url='https://legacy.example/v1')
        self._neutralize()
        Connection = self.env['im_livechat.ai.connection'].with_context(active_test=False)
        for connection in Connection.search([]):
            self.assertFalse(connection.active)
            self.assertFalse(connection.endpoint_url)
            self.assertFalse(connection.api_key_set)
            self.assertEqual(connection.customer_safe, connection.backend_type == 'openai')
        self.env.cr.execute("SELECT count(*) FROM im_livechat_channel WHERE ai_api_token_secret IS NOT NULL "
                            "OR ai_endpoint_url IS NOT NULL")
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        self.assertTrue(channel.ai_enabled, 'the switch stays as it was')
        self.assertFalse(channel._ai_connection_is_ready())

    def test_database_without_the_new_tables(self):
        """A copy still on 18.0.1.x, neutralized with the 18.0.2.0.0 code."""
        cr = self.env.cr
        cr.execute("ALTER TABLE im_livechat_ai_connection RENAME TO im_livechat_ai_connection_hidden")
        self._neutralize()
        cr.execute("ALTER TABLE im_livechat_ai_connection_hidden RENAME TO im_livechat_ai_connection")
        self.env.cr.execute("SELECT 1")
