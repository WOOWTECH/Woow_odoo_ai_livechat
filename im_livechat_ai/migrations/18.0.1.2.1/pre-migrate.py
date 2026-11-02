"""Remove only the retired 1.1.0 Settings view before Odoo validates views.

The 1.2.0 source removed its Python settings fields and XML data file. Odoo
normally removes retired XML records at the end of the module load, which is
too late for the inherited view that still names livechat_ai_base_url.
"""

import json
import logging
from xml.etree import ElementTree

_logger = logging.getLogger(__name__)

MODULE = 'im_livechat_ai'
XMLID = 'res_config_settings_view_form_livechat_ai'
VIEW_NAME = 'res.config.settings.view.form.inherit.im_livechat_ai'
VIEW_MODEL = 'res.config.settings'
LEGACY_FIELD = 'livechat_ai_base_url'


def _has_owned_legacy_field(arch_text):
    """Require the real field under our app in every stored language arch."""
    try:
        arches = json.loads(arch_text)
        if not isinstance(arches, dict) or not arches:
            return False
        for xml in arches.values():
            if not isinstance(xml, str):
                return False
            root = ElementTree.fromstring(xml)
            if not any(
                field.get('name') == LEGACY_FIELD
                for app in root.iter('app') if app.get('name') == MODULE
                for field in app.iter('field')
            ):
                return False
    except (TypeError, ValueError, ElementTree.ParseError):
        return False
    return True


def _assert_no_view_fk_references(cr, view_id):
    """Hold any FK row that deletion could restrict, null, default, or cascade.

    PostgreSQL quotes catalog-sourced identifiers before Python assembles each
    probe. The target view row is already locked FOR UPDATE by the caller.
    """
    cr.execute('''
        SELECT pg_catalog.format('%I.%I', ns.nspname, tbl.relname),
               pg_catalog.format('%I', src.attname), dst.attname,
               array_length(fk.conkey, 1), array_length(fk.confkey, 1)
          FROM pg_constraint AS fk
          JOIN pg_class AS tbl ON tbl.oid = fk.conrelid
          JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
          JOIN pg_attribute AS src ON src.attrelid = fk.conrelid
                                   AND src.attnum = fk.conkey[1]
          JOIN pg_attribute AS dst ON dst.attrelid = fk.confrelid
                                   AND dst.attnum = fk.confkey[1]
         WHERE fk.contype = 'f' AND fk.confrelid = 'ir_ui_view'::regclass
         ORDER BY fk.oid
    ''')
    for table_sql, column_sql, target_column, source_count, target_count in cr.fetchall():
        if source_count != 1 or target_count != 1 or target_column != 'id':
            raise RuntimeError('im_livechat_ai: unsupported Settings view FK; manual review required')
        cr.execute(f'SELECT 1 FROM {table_sql} WHERE {column_sql} = %s LIMIT 1', (view_id,))
        if cr.fetchone():
            raise RuntimeError('im_livechat_ai: retired Settings view has FK references; manual review required')


def migrate(cr, version):
    if not version:  # new installation has no legacy record
        return

    cr.execute('''
        SELECT d.id, v.id, v.arch_db::text,
               v.name = %s AND v.model = %s
                 AND v.inherit_id = (
                     SELECT base.res_id FROM ir_model_data AS base
                      WHERE base.module = 'base'
                        AND base.name = 'res_config_settings_view_form'
                        AND base.model = 'ir.ui.view'
                 ) AS is_legacy_view,
               v.arch_updated IS TRUE,
               EXISTS (SELECT 1 FROM ir_ui_view AS child WHERE child.inherit_id = v.id),
               EXISTS (SELECT 1 FROM ir_model_data AS alias
                        WHERE alias.model = 'ir.ui.view'
                          AND alias.res_id = v.id AND alias.id != d.id),
               EXISTS (SELECT 1 FROM ir_ui_view_custom AS custom
                        WHERE custom.ref_id = v.id)
          FROM ir_model_data AS d
          JOIN ir_ui_view AS v ON v.id = d.res_id
         WHERE d.module = %s AND d.name = %s AND d.model = 'ir.ui.view'
         FOR UPDATE OF v
    ''', (VIEW_NAME, VIEW_MODEL, MODULE, XMLID))
    target = cr.fetchone()
    if target is None:
        return

    (xmlid_id, view_id, arch_text, is_legacy_view, arch_updated,
     has_children, has_other_xmlids, has_custom_views) = target
    if not is_legacy_view or not _has_owned_legacy_field(arch_text):
        raise RuntimeError('im_livechat_ai: retired Settings XMLID has an unfamiliar view shape; '
                           'manual review required')
    if arch_updated or has_children or has_other_xmlids or has_custom_views:
        raise RuntimeError('im_livechat_ai: retired Settings view was edited or has dependants; manual review required')

    _assert_no_view_fk_references(cr, view_id)

    cr.execute('''
        DELETE FROM ir_ui_view AS v
         WHERE v.id = %s AND v.name = %s AND v.model = %s
           AND v.inherit_id = (
               SELECT base.res_id FROM ir_model_data AS base
                WHERE base.module = 'base'
                  AND base.name = 'res_config_settings_view_form'
                  AND base.model = 'ir.ui.view'
           )
           AND v.arch_db::text = %s
           AND v.arch_updated IS NOT TRUE
           AND EXISTS (SELECT 1 FROM ir_model_data AS d
                        WHERE d.id = %s AND d.module = %s AND d.name = %s
                          AND d.model = 'ir.ui.view' AND d.res_id = v.id)
           AND NOT EXISTS (SELECT 1 FROM ir_ui_view AS child WHERE child.inherit_id = v.id)
           AND NOT EXISTS (SELECT 1 FROM ir_model_data AS alias
                            WHERE alias.model = 'ir.ui.view' AND alias.res_id = v.id
                              AND alias.id != %s)
           AND NOT EXISTS (SELECT 1 FROM ir_ui_view_custom AS custom
                            WHERE custom.ref_id = v.id)
    ''', (view_id, VIEW_NAME, VIEW_MODEL, arch_text, xmlid_id, MODULE, XMLID, xmlid_id))
    if cr.rowcount != 1:
        raise RuntimeError('im_livechat_ai: retired Settings view changed during pre-migration')

    cr.execute('''
        DELETE FROM ir_model_data
         WHERE id = %s AND module = %s AND name = %s
           AND model = 'ir.ui.view' AND res_id = %s
    ''', (xmlid_id, MODULE, XMLID, view_id))
    if cr.rowcount != 1:
        raise RuntimeError('im_livechat_ai: retired Settings XMLID changed during pre-migration')
    _logger.info('im_livechat_ai: removed one retired Settings view before view validation')
