"""Remove only the retired 1.1.0 Settings view before Odoo validates views.

The 1.2.0 source removed its Python settings fields and XML data file. Odoo
normally removes retired XML records at the end of the module load, which is
too late for the inherited view that still names livechat_ai_base_url.
"""

import hashlib
import json
import logging
import re
from xml.etree import ElementTree

_logger = logging.getLogger(__name__)

MODULE = 'im_livechat_ai'
XMLID = 'res_config_settings_view_form_livechat_ai'
VIEW_NAME = 'res.config.settings.view.form.inherit.im_livechat_ai'
VIEW_MODEL = 'res.config.settings'
# The nine res.config.settings fields in the retired 1.1.0 view were all
# removed in 1.2.0. A sibling referencing any one can break later validation.
REMOVED_SETTINGS_FIELDS = frozenset({
    'livechat_ai_api_key', 'livechat_ai_api_key_set', 'livechat_ai_base_url',
    'livechat_ai_cron_lastcall', 'livechat_ai_job_warning', 'livechat_ai_log_payload',
    'livechat_ai_log_retention_days', 'livechat_ai_model_list', 'livechat_ai_stale_job_count',
})
REMOVED_FIELD_TOKEN = re.compile(
    r'(?<![A-Za-z0-9_])(?:' + '|'.join(map(re.escape, sorted(REMOVED_SETTINGS_FIELDS)))
    + r')(?![A-Za-z0-9_])')
REFERENCE_ATTRIBUTES = frozenset({'name', 'for', 'field', 'data-field', 'data-oe-field'})
EXPRESSION_ATTRIBUTES = frozenset({
    'invisible', 'readonly', 'required', 'column_invisible', 'attrs', 'modifiers',
    'domain', 'context', 'options', 'filter_domain', 't-if', 't-elif', 't-esc', 't-out',
})
# SHA-256 of the element/attribute tree in v18.0.1.1.0's
# views/res_config_settings_views.xml, record XMLID above. Human-facing text
# and Odoo-translated attributes are excluded; executable attributes, element
# order and every field/button remain exact. Unknown historic shapes stop.
LEGACY_STRUCTURE_SHA256 = '71c3d579067f26b511f898ed4b758d15d2289027bc541a48216f3d7695feb251'
DISPLAY_ATTRIBUTES = frozenset({'string', 'help', 'title', 'placeholder', 'data-string'})


def _structure(node):
    """Ignore only display translations and formatting, never extra XML nodes."""
    if (not isinstance(node.tag, str)
            or (node.tag != 'div' and (node.text or '').strip())
            or (node.tail or '').strip()):
        raise ValueError('unexpected Settings view XML node or text')
    return [
        node.tag,
        sorted((key, value) for key, value in node.attrib.items()
               if key not in DISPLAY_ATTRIBUTES),
        [_structure(child) for child in node],
    ]


def _has_expected_legacy_structure(arch_text):
    """Every stored language must match the frozen 1.1.0 tree exactly."""
    try:
        arches = json.loads(arch_text)
        if not isinstance(arches, dict) or not arches:
            return False
        for xml in arches.values():
            if not isinstance(xml, str):
                return False
            root = ElementTree.fromstring(
                xml, parser=ElementTree.XMLParser(
                    target=ElementTree.TreeBuilder(insert_comments=True)))
            shape = json.dumps(_structure(root), ensure_ascii=False, separators=(',', ':'))
            if hashlib.sha256(shape.encode()).hexdigest() != LEGACY_STRUCTURE_SHA256:
                return False
    except (TypeError, ValueError, ElementTree.ParseError):
        return False
    return True


def _is_expression_attribute(name):
    return name in EXPRESSION_ATTRIBUTES or name.startswith(
        ('decoration-', 't-att-', 't-attf-'))


def _node_references_removed_field(node):
    """Inspect parsed XML locators, labels, modifiers and attribute changes."""
    for name, value in node.attrib.items():
        if name in REFERENCE_ATTRIBUTES and value in REMOVED_SETTINGS_FIELDS:
            return True
        is_expression = (node.tag == 'xpath' and name == 'expr') or _is_expression_attribute(name)
        if is_expression and REMOVED_FIELD_TOKEN.search(value):
            return True
    if node.tag == 'attribute':
        changed_name = node.get('name', '')
        is_reference_change = changed_name in REFERENCE_ATTRIBUTES or _is_expression_attribute(changed_name)
        if is_reference_change and any(
                REMOVED_FIELD_TOKEN.search(value or '') for value in (
                    node.text, node.get('add'), node.get('remove'))):
            return True
    return False


def _contains_removed_settings_field(arch_text):
    """Find structural references in every stored Settings view language.

    An exact retired-field token in an XPath or expression may be a quoted
    display literal rather than a live reference. Hold such cases for manual
    review rather than risking a broken post-migration Settings form.
    """
    try:
        arches = json.loads(arch_text)
        if not isinstance(arches, dict) or not arches:
            raise ValueError('Settings view arch_db is not a language mapping')
        for xml in arches.values():
            if not isinstance(xml, str):
                raise TypeError('Settings view language architecture is not XML text')
            root = ElementTree.fromstring(xml)
            if any(_node_references_removed_field(node) for node in root.iter()):
                return True
    except (TypeError, ValueError, ElementTree.ParseError) as exc:
        raise RuntimeError('im_livechat_ai: unparseable Settings view architecture; '
                           'manual review required') from exc
    return False


def _assert_no_residual_legacy_view(cr):
    """Hold any residual reference or unparseable Settings view architecture.

    Scan only the Settings model. This runs when the target XMLID is absent
    and again after deleting the exact target in the same transaction.
    """
    cr.execute('''
        SELECT v.name, v.arch_db::text
          FROM ir_ui_view AS v
         WHERE v.model = %s
    ''', (VIEW_MODEL,))
    for name, arch_text in cr.fetchall():
        if name == VIEW_NAME or _contains_removed_settings_field(arch_text):
            raise RuntimeError('im_livechat_ai: retired Settings view remains after cleanup; '
                               'manual review required')


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
        _assert_no_residual_legacy_view(cr)
        return

    (xmlid_id, view_id, arch_text, is_legacy_view, arch_updated,
     has_children, has_other_xmlids, has_custom_views) = target
    if not is_legacy_view or not _has_expected_legacy_structure(arch_text):
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
    _assert_no_residual_legacy_view(cr)
    _logger.info('im_livechat_ai: removed one retired Settings view before view validation')
