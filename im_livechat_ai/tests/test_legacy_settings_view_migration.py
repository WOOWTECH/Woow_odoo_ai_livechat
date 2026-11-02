"""Isolated SQL reproduction for the removed 1.1.0 Settings view.

SQLite runs the migration SQL in a disposable database; the adapter rewrites
PostgreSQL's JSONB-to-text cast, row lock and placeholders, and emulates the
FK catalog from SQLite's own constraints. A full Odoo/PostgreSQL upgrade
remains a separate gate.
"""

import ast
import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
LEGACY_VIEW = 6510
LEGACY_XMLID = 510
ARCH = (MODULE / 'tests/fixtures/legacy-settings-arch-18.0.1.1.0.xml').read_text().strip()


def arch_json(*xml_values):
    return json.dumps({f'lang_{index}': xml for index, xml in enumerate(xml_values or (ARCH,))})


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


class CatalogRows:
    def __init__(self, rows):
        self.rows = iter(rows)

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.rowcount = -1
        self.queries = []
        self.after_fetch = None

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if 'FROM pg_constraint AS fk' in query:
            self.result = CatalogRows(self.foreign_keys_to_views())
            self.rowcount = -1
            return
        self.result = self.connection.execute(
            query.replace('v.arch_db::text', 'v.arch_db').replace('FOR UPDATE OF v', '').replace('%s', '?'),
            params,
        )
        self.rowcount = self.result.rowcount

    def foreign_keys_to_views(self):
        rows = []
        tables = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        ).fetchall()
        for (table,) in tables:
            fks = self.connection.execute(f'PRAGMA foreign_key_list({quote_identifier(table)})').fetchall()
            by_id = {}
            for fk in fks:
                by_id.setdefault(fk[0], []).append(fk)
            for parts in by_id.values():
                if parts[0][2] != 'ir_ui_view':
                    continue
                rows.append((
                    f'"main".{quote_identifier(table)}', quote_identifier(parts[0][3]),
                    parts[0][4], len(parts), len(parts),
                ))
        return rows

    def fetchone(self):
        result = self.result.fetchone()
        if self.after_fetch:
            callback, self.after_fetch = self.after_fetch, None
            callback()
        return result

    def fetchall(self):
        return self.result.fetchall()


def version(value):
    return tuple(int(part) for part in value.split('.'))


def run_view_pre_migrations(cursor, installed_version):
    manifest = ast.literal_eval((MODULE / '__manifest__.py').read_text())
    current = version(manifest['version'])
    for directory in sorted((MODULE / 'migrations').iterdir(), key=lambda path: version(path.name)):
        if not version(installed_version) < version(directory.name) <= current:
            continue
        # The 1.2.0 pre-script adds an unrelated channel column through Odoo's
        # SQL helper; the fixture models only the Settings view tables.
        if directory.name == '18.0.1.2.0':
            continue
        path = directory / 'pre-migrate.py'
        if path.exists():
            spec = importlib.util.spec_from_file_location('legacy_view_pre_migrate', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.migrate(cursor, installed_version)


class TestLegacySettingsViewMigration(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.executescript('''
            CREATE TABLE ir_ui_view (
                id INTEGER PRIMARY KEY, name TEXT, model TEXT, inherit_id INTEGER,
                arch_db TEXT, arch_updated INTEGER,
                FOREIGN KEY(inherit_id) REFERENCES ir_ui_view(id)
            );
            CREATE TABLE ir_model_data (
                id INTEGER PRIMARY KEY, module TEXT, name TEXT, model TEXT,
                res_id INTEGER, UNIQUE(module, name)
            );
            CREATE TABLE ir_ui_view_custom (
                id INTEGER PRIMARY KEY, ref_id INTEGER NOT NULL,
                FOREIGN KEY(ref_id) REFERENCES ir_ui_view(id) ON DELETE CASCADE
            );
            CREATE TABLE ir_act_window_view (
                id INTEGER PRIMARY KEY, view_id INTEGER,
                FOREIGN KEY(view_id) REFERENCES ir_ui_view(id) ON DELETE SET NULL
            );
            CREATE TABLE ir_ui_view_group_rel (
                group_id INTEGER, view_id INTEGER,
                FOREIGN KEY(view_id) REFERENCES ir_ui_view(id) ON DELETE CASCADE
            );
        ''')
        self.db.execute('INSERT INTO ir_ui_view VALUES (1, ?, ?, NULL, ?, NULL)',
                        ('base.settings', 'res.config.settings', arch_json('<form/>')))
        self.db.execute("INSERT INTO ir_model_data VALUES (1, 'base', 'res_config_settings_view_form', 'ir.ui.view', 1)")
        self.db.execute('INSERT INTO ir_ui_view VALUES (?, ?, ?, 1, ?, NULL)', (
            LEGACY_VIEW, 'res.config.settings.view.form.inherit.im_livechat_ai',
            'res.config.settings', arch_json(ARCH, ARCH),
        ))
        self.db.execute('INSERT INTO ir_model_data VALUES (?, ?, ?, ?, ?)', (
            LEGACY_XMLID, 'im_livechat_ai', 'res_config_settings_view_form_livechat_ai',
            'ir.ui.view', LEGACY_VIEW,
        ))
        self.db.execute('INSERT INTO ir_ui_view VALUES (6520, ?, ?, 1, ?, NULL)',
                        ('unrelated', 'res.config.settings', arch_json('<form/>')))
        self.db.execute("INSERT INTO ir_model_data VALUES (520, 'other_module', 'unrelated_settings', 'ir.ui.view', 6520)")
        self.cursor = Cursor(self.db)

    def tearDown(self):
        self.db.close()

    def assert_legacy_view_validates(self):
        if self.db.execute('''SELECT 1 FROM ir_ui_view WHERE model = 'res.config.settings'
                             AND arch_db LIKE '%livechat_ai_base_url%' ''').fetchone():
            raise AssertionError("Field 'livechat_ai_base_url' does not exist in model 'res.config.settings'")

    def assert_target_remains(self):
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,)).fetchone())

    def test_failed_1_2_0_upgrade_runs_guarded_1_2_1_script_on_1_2_2(self):
        with self.assertRaisesRegex(AssertionError, 'livechat_ai_base_url'):
            self.assert_legacy_view_validates()
        run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_legacy_view_validates()
        self.assertIsNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNone(self.db.execute('SELECT id FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = 6520').fetchone())
        run_view_pre_migrations(self.cursor, '18.0.1.2.0')  # retry is a no-op
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = 6520').fetchone())

    def test_1_1_0_to_1_2_2_removes_the_same_legacy_view(self):
        run_view_pre_migrations(self.cursor, '18.0.1.1.0')
        self.assert_legacy_view_validates()
        self.assertIsNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = 6520').fetchone())

    def test_new_install_does_not_query_or_delete(self):
        for version in ('18.0.1.2.1', '18.0.1.2.2'):
            path = MODULE / 'migrations' / version / 'pre-migrate.py'
            spec = importlib.util.spec_from_file_location('legacy_view_new_install', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.migrate(self.cursor, None)
        self.assertEqual(self.cursor.queries, [])
        self.assert_target_remains()

    def test_wrong_xmlid_identity_cannot_delete_a_view(self):
        for column, value in (('module', 'other_module'), ('name', 'other_settings'),
                              ('model', 'ir.actions.act_window')):
            with self.subTest(column=column):
                self.db.execute(f'UPDATE ir_model_data SET {column} = ? WHERE id = ?',
                                (value, LEGACY_XMLID))
                with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                    run_view_pre_migrations(self.cursor, '18.0.1.2.0')
                self.assert_target_remains()
                original = {'module': 'im_livechat_ai',
                            'name': 'res_config_settings_view_form_livechat_ai',
                            'model': 'ir.ui.view'}[column]
                self.db.execute(f'UPDATE ir_model_data SET {column} = ? WHERE id = ?',
                                (original, LEGACY_XMLID))

    def test_repurposed_view_shape_is_not_deleted(self):
        changes = (
            ('name', 'custom.settings'), ('model', 'res.partner'),
            ('inherit_id', None), ('arch_db', arch_json('<form><field name="name"/></form>')),
        )
        originals = {
            'name': 'res.config.settings.view.form.inherit.im_livechat_ai',
            'model': 'res.config.settings', 'inherit_id': 1, 'arch_db': arch_json(ARCH, ARCH),
        }
        for column, value in changes:
            with self.subTest(column=column):
                self.db.execute(f'UPDATE ir_ui_view SET {column} = ? WHERE id = ?',
                                (value, LEGACY_VIEW))
                with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                    run_view_pre_migrations(self.cursor, '18.0.1.2.0')
                self.assert_target_remains()
                self.db.execute(f'UPDATE ir_ui_view SET {column} = ? WHERE id = ?',
                                (originals[column], LEGACY_VIEW))

    def test_child_view_or_second_xmlid_requires_manual_review(self):
        self.db.execute("INSERT INTO ir_ui_view VALUES (6600, 'child', 'res.config.settings', 6510, '<form/>', NULL)")
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()
        self.db.execute('DELETE FROM ir_ui_view WHERE id = 6600')
        self.db.execute("INSERT INTO ir_model_data VALUES (511, 'other_module', 'alias', 'ir.ui.view', 6510)")
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()

    def test_installed_1_2_1_residual_view_runs_guarded_1_2_2_migration(self):
        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assertIsNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNone(self.db.execute('SELECT 1 FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6520').fetchone())

    def test_extra_field_holds_on_both_upgrade_paths(self):
        extra = ARCH.replace('<field name="livechat_ai_base_url"',
                             '<field name="unrelated_custom_field"/><field name="livechat_ai_base_url"')
        self.assertNotEqual(extra, ARCH)
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed):
                self.db.execute('UPDATE ir_ui_view SET arch_db = ?, arch_updated = 0 WHERE id = ?',
                                (arch_json(ARCH, extra), LEGACY_VIEW))
                with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                    run_view_pre_migrations(self.cursor, installed)
                self.assert_target_remains()

    def test_nontranslated_attribute_change_holds(self):
        changed = ARCH.replace('invisible="not livechat_ai_api_key_set"', 'invisible="0"')
        self.assertNotEqual(changed, ARCH)
        self.db.execute('UPDATE ir_ui_view SET arch_db = ?, arch_updated = 0 WHERE id = ?',
                        (arch_json(changed), LEGACY_VIEW))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()

    def test_translated_labels_keep_exact_structure(self):
        translated = (ARCH.replace('string="Live Chat AI"', 'string="即時聊天 AI"')
                         .replace('title="LiteLLM Connection"', 'title="LiteLLM 連線"')
                         .replace('A key is configured (it is never displayed).', '金鑰已設定。'))
        self.assertNotEqual(translated, ARCH)
        self.db.execute('UPDATE ir_ui_view SET arch_db = ? WHERE id = ?',
                        (arch_json(ARCH, translated), LEGACY_VIEW))
        run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assertIsNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())

    def test_installed_1_2_1_without_legacy_view_upgrades_to_1_2_2_without_cleanup(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assertFalse(any('DELETE' in query.upper() for query, _ in self.cursor.queries))
        self.assertIsNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6520').fetchone())

    def test_comment_or_unrelated_app_does_not_authorize_deletion(self):
        other_arches = (
            '<xpath><app name="im_livechat_ai"><!-- <field name="livechat_ai_base_url"/> --></app></xpath>',
            '<xpath><app name="other"><field name="livechat_ai_base_url"/></app></xpath>',
            '<xpath><field name="livechat_ai_base_url"/><app name="im_livechat_ai"/></xpath>',
            '<xpath><app name="im_livechat_ai"><field name="livechatXaiXbaseXurl"/></app></xpath>',
            '<xpath><app name="im_livechat_ai"><field name="livechat_ai_base_url"></xpath>',
        )
        for xml in other_arches:
            with self.subTest(xml=xml):
                self.db.execute('UPDATE ir_ui_view SET arch_db = ? WHERE id = ?',
                                (arch_json(xml), LEGACY_VIEW))
                with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                    run_view_pre_migrations(self.cursor, '18.0.1.2.0')
                self.assert_target_remains()

    def test_every_language_arch_must_have_real_field_in_owned_app(self):
        self.db.execute('UPDATE ir_ui_view SET arch_db = ? WHERE id = ?', (
            arch_json(ARCH, '<xpath><app name="im_livechat_ai"><!-- livechat_ai_base_url --></app></xpath>'),
            LEGACY_VIEW,
        ))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()

    def test_arch_updated_requires_manual_review(self):
        self.db.execute('UPDATE ir_ui_view SET arch_updated = 1 WHERE id = ?', (LEGACY_VIEW,))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()

    def test_custom_view_requires_manual_review_and_remains(self):
        self.db.execute('INSERT INTO ir_ui_view_custom VALUES (8000, ?)', (LEGACY_VIEW,))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view_custom WHERE id = 8000').fetchone())

    def test_arch_changed_between_read_and_delete_aborts_without_deleting(self):
        changed_arch = arch_json(ARCH.replace('id="livechat_ai_base_url_setting"', 'id="changed"'))
        self.cursor.after_fetch = lambda: self.db.execute(
            'UPDATE ir_ui_view SET arch_db = ? WHERE id = ?', (changed_arch, LEGACY_VIEW),
        )
        with self.assertRaisesRegex(RuntimeError, 'changed during pre-migration'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()

    def test_manual_edit_between_read_and_delete_aborts_without_deleting(self):
        self.cursor.after_fetch = lambda: self.db.execute(
            'UPDATE ir_ui_view SET arch_updated = 1 WHERE id = ?', (LEGACY_VIEW,),
        )
        with self.assertRaisesRegex(RuntimeError, 'changed during pre-migration'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()

    def test_custom_view_added_between_read_and_delete_is_preserved(self):
        self.cursor.after_fetch = lambda: self.db.execute(
            'INSERT INTO ir_ui_view_custom VALUES (8000, ?)', (LEGACY_VIEW,),
        )
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view_custom WHERE id = 8000').fetchone())

    def test_action_view_set_null_reference_requires_manual_review(self):
        self.db.execute('INSERT INTO ir_act_window_view VALUES (9000, ?)', (LEGACY_VIEW,))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()
        self.assertEqual(self.db.execute('SELECT view_id FROM ir_act_window_view WHERE id = 9000').fetchone()[0],
                         LEGACY_VIEW)

    def test_group_relation_cascade_reference_requires_manual_review(self):
        self.db.execute('INSERT INTO ir_ui_view_group_rel VALUES (42, ?)', (LEGACY_VIEW,))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()
        self.assertIsNotNone(self.db.execute(
            'SELECT 1 FROM ir_ui_view_group_rel WHERE group_id = 42 AND view_id = ?',
            (LEGACY_VIEW,),
        ).fetchone())

    def test_unlisted_quoted_fk_table_is_discovered(self):
        self.db.execute('''CREATE TABLE "quote"" table" (
            id INTEGER PRIMARY KEY, "view""id" INTEGER REFERENCES ir_ui_view(id) ON DELETE CASCADE
        )''')
        self.db.execute('INSERT INTO "quote"" table" VALUES (1, ?)', (LEGACY_VIEW,))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM "quote"" table" WHERE id = 1').fetchone())

    def test_missing_xmlid_with_residual_view_holds(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed), self.assertRaisesRegex(
                    RuntimeError, 'manual review required'):
                run_view_pre_migrations(self.cursor, installed)
        self.assertFalse(any('FROM pg_constraint AS fk' in query for query, _ in self.cursor.queries))
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = 6520').fetchone())

    def test_renamed_residual_view_without_xmlid_holds(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute("UPDATE ir_ui_view SET name = 'renamed legacy view' WHERE id = ?", (LEGACY_VIEW,))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())

    def test_sibling_with_removed_field_rolls_back_cleanup_on_both_paths(self):
        sibling_arch = arch_json('<xpath expr="//form" position="inside">'
                                 '<app name="another_app"><field name="livechat_ai_base_url"/>'
                                 '</app></xpath>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('sibling.settings', 'res.config.settings', sibling_arch))
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed):
                self.db.execute('SAVEPOINT migration_attempt')
                start = len(self.cursor.queries)
                with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                    run_view_pre_migrations(self.cursor, installed)
                queries = [query for query, _ in self.cursor.queries[start:]]
                deleted_xmlid_at = next(index for index, query in enumerate(queries)
                                        if 'DELETE FROM ir_model_data' in query)
                residual_scan_at = next(index for index, query in enumerate(queries)
                                        if 'SELECT v.name, v.arch_db::text' in query)
                self.assertLess(deleted_xmlid_at, residual_scan_at)
                self.db.execute('ROLLBACK TO SAVEPOINT migration_attempt')
                self.db.execute('RELEASE SAVEPOINT migration_attempt')
                self.assert_target_remains()
                self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())

    def test_sibling_with_other_retired_settings_field_also_holds(self):
        sibling_arch = arch_json('<xpath><field name="livechat_ai_api_key"/></xpath>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('sibling.settings', 'res.config.settings', sibling_arch))
        self.db.execute('SAVEPOINT migration_attempt')
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.db.execute('ROLLBACK TO SAVEPOINT migration_attempt')
        self.db.execute('RELEASE SAVEPOINT migration_attempt')
        self.assert_target_remains()

    def test_xpath_only_sibling_rolls_back_cleanup_on_both_paths(self):
        # The removed field occurs only in an XPath expression, not a field node.
        sibling_arch = arch_json(
            '<xpath expr="//field[@name=\'livechat_ai_base_url\']" position="attributes">'
            '<attribute name="string">Old setting</attribute></xpath>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('sibling.settings', 'res.config.settings', sibling_arch))
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed):
                self.db.execute('SAVEPOINT migration_attempt')
                start = len(self.cursor.queries)
                with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                    run_view_pre_migrations(self.cursor, installed)
                queries = [query for query, _ in self.cursor.queries[start:]]
                deleted_xmlid_at = next(index for index, query in enumerate(queries)
                                        if 'DELETE FROM ir_model_data' in query)
                residual_scan_at = next(index for index, query in enumerate(queries)
                                        if 'SELECT v.name, v.arch_db::text' in query)
                self.assertLess(deleted_xmlid_at, residual_scan_at)
                self.db.execute('ROLLBACK TO SAVEPOINT migration_attempt')
                self.db.execute('RELEASE SAVEPOINT migration_attempt')
                self.assert_target_remains()
                self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())

    def test_xpath_only_residual_holds_when_target_is_absent_on_both_paths(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        sibling_arch = arch_json(
            '<xpath expr="//field[@name=\'livechat_ai_api_key\']" position="attributes"/>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('sibling.settings', 'res.config.settings', sibling_arch))
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed), self.assertRaisesRegex(
                    RuntimeError, 'manual review required'):
                run_view_pre_migrations(self.cursor, installed)
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())

    def test_xpath_similar_literal_and_comment_do_not_hold(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        decoy = arch_json(
            '<xpath expr="//field[@name=\'livechat_ai_base_url_extra\']" position="attributes"/>'
            '<!-- //field[@name=\'livechat_ai_base_url\'] -->')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('decoy.settings', 'res.config.settings', decoy))
        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())

    def test_structural_references_hold_on_both_upgrade_paths(self):
        cases = (
            ('label', '<xpath><label for="livechat_ai_base_url"/></xpath>'),
            ('modifier', '<xpath><div invisible="not livechat_ai_api_key_set"/></xpath>'),
            ('attribute_mutation', ('<xpath expr="//form" position="attributes">'
                                    '<attribute name="invisible">livechat_ai_base_url</attribute></xpath>')),
            ('attribute_add', ('<xpath expr="//form" position="attributes">'
                               '<attribute name="invisible" add="not livechat_ai_base_url"/></xpath>')),
            ('modifiers', ('<xpath><div modifiers="{&quot;invisible&quot;: '
                           '[[&quot;livechat_ai_base_url&quot;, &quot;=&quot;, true]]}"/></xpath>')),
            ('xpath_double_quotes',
             '<xpath expr=\'//field[@name="livechat_ai_base_url"]\' position="attributes"/>'),
        )
        for case, xml in cases:
            with self.subTest(case=case):
                self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                                ('sibling.settings', 'res.config.settings', arch_json(xml)))
                try:
                    for installed in ('18.0.1.2.0', '18.0.1.2.1'):
                        with self.subTest(installed=installed):
                            self.db.execute('SAVEPOINT migration_attempt')
                            try:
                                with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                                    run_view_pre_migrations(self.cursor, installed)
                            finally:
                                self.db.execute('ROLLBACK TO SAVEPOINT migration_attempt')
                                self.db.execute('RELEASE SAVEPOINT migration_attempt')
                            self.assert_target_remains()
                finally:
                    self.db.execute('DELETE FROM ir_ui_view WHERE id = 6600')

    def test_nonprimary_translation_structural_reference_holds(self):
        translated = arch_json('<xpath expr="//form" position="attributes"/>',
                               '<xpath><label for="livechat_ai_base_url"/></xpath>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('sibling.settings', 'res.config.settings', translated))
        self.db.execute('SAVEPOINT migration_attempt')
        try:
            with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        finally:
            self.db.execute('ROLLBACK TO SAVEPOINT migration_attempt')
            self.db.execute('RELEASE SAVEPOINT migration_attempt')
        self.assert_target_remains()

    def test_unparseable_settings_arch_holds_before_commit(self):
        malformed = ('not-json', arch_json('<form/>', '<xpath><field name="other"/>'))
        for arch_text in malformed:
            with self.subTest(arch_text=arch_text[:12]):
                self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                                ('sibling.settings', 'res.config.settings', arch_text))
                self.db.execute('SAVEPOINT migration_attempt')
                try:
                    with self.assertRaisesRegex(RuntimeError, 'manual review required'):
                        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
                finally:
                    self.db.execute('ROLLBACK TO SAVEPOINT migration_attempt')
                    self.db.execute('RELEASE SAVEPOINT migration_attempt')
                self.assert_target_remains()
                self.db.execute('DELETE FROM ir_ui_view WHERE id = 6600')

    def test_similar_structural_names_do_not_hold_without_target(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        decoy = arch_json('<xpath><label for="livechat_ai_base_url_extra"/>'
                          '<div invisible="not livechat_ai_api_key_set_extra"/>'
                          '<attribute name="help">livechat_ai_base_url</attribute></xpath>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('decoy.settings', 'res.config.settings', decoy))
        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())

    def test_xpath_literal_in_other_predicate_conservatively_holds(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        arch = arch_json('<xpath expr="//div[@data-name=\'livechat_ai_base_url\']" '
                         'position="attributes"/>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('sibling.settings', 'res.config.settings', arch))
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.1')

    def test_unparseable_settings_arch_holds_without_target(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        arch = arch_json('<form/>', 42)
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('sibling.settings', 'res.config.settings', arch))
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed), self.assertRaisesRegex(
                    RuntimeError, 'manual review required'):
                run_view_pre_migrations(self.cursor, installed)

    def test_unparseable_unrelated_model_arch_is_outside_scan(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, NULL, ?, 0)',
                        ('unrelated', 'res.partner', 'not-json'))
        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())

    def test_missing_xmlid_renamed_view_and_app_with_removed_field_holds(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        renamed_arch = ARCH.replace('name="im_livechat_ai"', 'name="another_app"')
        self.assertNotEqual(renamed_arch, ARCH)
        self.db.execute('UPDATE ir_ui_view SET name = ?, arch_db = ? WHERE id = ?',
                        ('renamed.settings', arch_json(renamed_arch), LEGACY_VIEW))
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed), self.assertRaisesRegex(
                    RuntimeError, 'manual review required'):
                run_view_pre_migrations(self.cursor, installed)
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = ?',
                                             (LEGACY_VIEW,)).fetchone())

    def test_residual_scan_ignores_same_field_in_unrelated_model(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        unrelated_arch = arch_json('<form><field name="livechat_ai_base_url"/></form>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, NULL, ?, 0)',
                        ('res.config.settings.view.form.inherit.im_livechat_ai',
                         'res.partner', unrelated_arch))
        for installed in ('18.0.1.2.0', '18.0.1.2.1'):
            with self.subTest(installed=installed):
                run_view_pre_migrations(self.cursor, installed)
        scans = [(query, params) for query, params in self.cursor.queries
                 if 'SELECT v.name, v.arch_db::text' in query]
        self.assertTrue(scans)
        self.assertTrue(all(params == ('res.config.settings',) for _, params in scans))
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())

    def test_residual_scan_ignores_comment_and_similar_field_name(self):
        self.db.execute('DELETE FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,))
        self.db.execute('DELETE FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,))
        decoy = arch_json('<xpath><!-- <field name="livechat_ai_base_url"/> -->'
                          '<field name="livechatXaiXbaseXurl"/></xpath>')
        self.db.execute('INSERT INTO ir_ui_view VALUES (6600, ?, ?, 1, ?, 0)',
                        ('decoy.settings', 'res.config.settings', decoy))
        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM ir_ui_view WHERE id = 6600').fetchone())


if __name__ == '__main__':
    unittest.main()
