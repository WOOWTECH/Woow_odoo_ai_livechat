"""Isolated SQL reproduction for the removed 1.1.0 Settings view.

SQLite runs the portable migration SQL in a disposable database. This checks
record identity and ordering, while a full Odoo/PostgreSQL upgrade remains a
separate gate.
"""

import ast
import importlib.util
import sqlite3
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
LEGACY_VIEW = 6510
LEGACY_XMLID = 510
ARCH = '<form><field name="livechat_ai_base_url"/></form>'


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.rowcount = -1
        self.queries = []

    def execute(self, query, params=()):
        self.queries.append((query, params))
        self.result = self.connection.execute(query.replace('%s', '?'), params)
        self.rowcount = self.result.rowcount

    def fetchone(self):
        return self.result.fetchone()


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
                arch_db TEXT, FOREIGN KEY(inherit_id) REFERENCES ir_ui_view(id)
            );
            CREATE TABLE ir_model_data (
                id INTEGER PRIMARY KEY, module TEXT, name TEXT, model TEXT,
                res_id INTEGER, UNIQUE(module, name)
            );
        ''')
        self.db.execute("INSERT INTO ir_ui_view VALUES (1, 'base.settings', 'res.config.settings', NULL, '<form/>')")
        self.db.execute("INSERT INTO ir_model_data VALUES (1, 'base', 'res_config_settings_view_form', 'ir.ui.view', 1)")
        self.db.execute('INSERT INTO ir_ui_view VALUES (?, ?, ?, 1, ?)', (
            LEGACY_VIEW, 'res.config.settings.view.form.inherit.im_livechat_ai',
            'res.config.settings', ARCH,
        ))
        self.db.execute('INSERT INTO ir_model_data VALUES (?, ?, ?, ?, ?)', (
            LEGACY_XMLID, 'im_livechat_ai', 'res_config_settings_view_form_livechat_ai',
            'ir.ui.view', LEGACY_VIEW,
        ))
        self.db.execute("INSERT INTO ir_ui_view VALUES (6520, 'unrelated', 'res.config.settings', 1, '<form/>')")
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

    def test_failed_1_2_0_upgrade_retries_through_1_2_1_before_view_validation(self):
        with self.assertRaisesRegex(AssertionError, 'livechat_ai_base_url'):
            self.assert_legacy_view_validates()
        run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_legacy_view_validates()
        self.assertIsNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNone(self.db.execute('SELECT id FROM ir_model_data WHERE id = ?', (LEGACY_XMLID,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = 6520').fetchone())
        run_view_pre_migrations(self.cursor, '18.0.1.2.0')  # retry is a no-op
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = 6520').fetchone())

    def test_1_1_0_to_1_2_1_removes_the_same_legacy_view(self):
        run_view_pre_migrations(self.cursor, '18.0.1.1.0')
        self.assert_legacy_view_validates()
        self.assertIsNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = ?', (LEGACY_VIEW,)).fetchone())
        self.assertIsNotNone(self.db.execute('SELECT id FROM ir_ui_view WHERE id = 6520').fetchone())

    def test_new_install_does_not_query_or_delete(self):
        path = MODULE / 'migrations/18.0.1.2.1/pre-migrate.py'
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
            ('inherit_id', None), ('arch_db', '<form><field name="name"/></form>'),
        )
        originals = {
            'name': 'res.config.settings.view.form.inherit.im_livechat_ai',
            'model': 'res.config.settings', 'inherit_id': 1, 'arch_db': ARCH,
        }
        for column, value in changes:
            with self.subTest(column=column):
                self.db.execute(f'UPDATE ir_ui_view SET {column} = ? WHERE id = ?',
                                (value, LEGACY_VIEW))
                run_view_pre_migrations(self.cursor, '18.0.1.2.0')
                self.assert_target_remains()
                self.db.execute(f'UPDATE ir_ui_view SET {column} = ? WHERE id = ?',
                                (originals[column], LEGACY_VIEW))

    def test_child_view_or_second_xmlid_requires_manual_review(self):
        self.db.execute("INSERT INTO ir_ui_view VALUES (6600, 'child', 'res.config.settings', 6510, '<form/>')")
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()
        self.db.execute('DELETE FROM ir_ui_view WHERE id = 6600')
        self.db.execute("INSERT INTO ir_model_data VALUES (511, 'other_module', 'alias', 'ir.ui.view', 6510)")
        with self.assertRaisesRegex(RuntimeError, 'manual review required'):
            run_view_pre_migrations(self.cursor, '18.0.1.2.0')
        self.assert_target_remains()

    def test_same_version_does_not_replay_a_versioned_migration(self):
        run_view_pre_migrations(self.cursor, '18.0.1.2.1')
        self.assert_target_remains()


if __name__ == '__main__':
    unittest.main()
