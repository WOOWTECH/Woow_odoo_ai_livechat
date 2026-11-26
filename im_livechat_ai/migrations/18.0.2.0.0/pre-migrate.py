# -*- coding: utf-8 -*-
"""18.0.2.0.0 pre-migration (SQL only; the new code is not loaded yet).

1. Refuse an upgrade from below 18.0.1.1.0: the 18.0.1.1.0 post-migration
   converts 18.0.1.0.x data through channel fields that 18.0.2.0.0 removes.
   Such a database is upgraded to 18.0.1.3.5 first.
2. Refuse while the removed Live Chat bridge of Discuss AI
   (``woow_ai_contact_livechat``) is installed: its views and data would stay
   without code.
3. The retired 18.0.1.1.0 Settings view (folded from the draft PR #6 guard):
   when ``im_livechat_ai.res_config_settings_view_form_livechat_ai`` still
   exists, it is deleted only when it is exactly that retired view (unedited,
   no child view, no other xmlid, no customization, the 18.0.1.1.0 structure);
   anything else stops the upgrade for a manual review, before another module
   validates the Settings form against fields that no longer exist (the
   production incident of 2026-11-02). Then the upgrade stops when any Settings
   view still names one of the removed ``livechat_ai_*`` fields. The 18.0.1.2.1
   script removes the view on the way up from 18.0.1.1.x; this step covers
   databases at 18.0.1.2.1 or later, where that script never runs again.
4. Bring back the xmlids of the default bot and its tag when the removed
   bridge moved them away.
5. Keep the moved 18.0.1.x channel columns for one version: their field
   reflections become noupdate, so the end of the upgrade does not drop them
   (a rollback to 18.0.1.3.5 finds its data). A later version drops them on
   purpose.

Only counts and ids are logged.
"""

import hashlib
import json
import logging
import re
from xml.etree import ElementTree

from odoo.exceptions import UserError
from odoo.tools import parse_version

_logger = logging.getLogger(__name__)

MODULE = 'im_livechat_ai'
MIN_VERSION = '18.0.1.1.0'
BRIDGE = 'woow_ai_contact_livechat'
BLOCKING_STATES = ('installed', 'to upgrade', 'to remove')

# --- 3. retired 18.0.1.1.0 Settings view (draft PR #6) ---
SETTINGS_XMLID = 'res_config_settings_view_form_livechat_ai'
SETTINGS_VIEW_NAME = 'res.config.settings.view.form.inherit.im_livechat_ai'
SETTINGS_MODEL = 'res.config.settings'
# the nine res.config.settings fields of that view, all removed by 18.0.1.2.0
REMOVED_SETTINGS_FIELDS = (
    'livechat_ai_api_key', 'livechat_ai_api_key_set', 'livechat_ai_base_url', 'livechat_ai_cron_lastcall',
    'livechat_ai_job_warning', 'livechat_ai_log_payload', 'livechat_ai_log_retention_days',
    'livechat_ai_model_list', 'livechat_ai_stale_job_count',
)
# SHA-256 of the element / attribute tree of v18.0.1.1.0's views/res_config_settings_views.xml record (display
# texts excluded, every element, field, button and executable attribute kept): the value of the draft PR #6
LEGACY_STRUCTURE_SHA256 = '71c3d579067f26b511f898ed4b758d15d2289027bc541a48216f3d7695feb251'
DISPLAY_ATTRIBUTES = frozenset({'string', 'help', 'title', 'placeholder', 'data-string'})

# --- 4. xmlids the removed bridge moved ---
BRIDGE_XMLIDS = (
    ('partner_ai_bot', (('woow_ai_contact_livechat', 'legacy_partner_ai_bot'),
                        ('__export__', 'woow_ai_contact_livechat_legacy_partner_ai_bot'))),
    ('partner_category_ai_bot', (('woow_ai_contact_livechat', 'legacy_partner_category_ai_bot'),
                                 ('__export__', 'woow_ai_contact_livechat_legacy_partner_category_ai_bot'))),
)

# --- 5. channel fields moved to the AI connection / assistant (or removed) whose columns stay one version ---
KEPT_FIELDS = ('ai_backend_type', 'ai_endpoint_url', 'ai_model', 'ai_document_mode', 'ai_system_prompt',
               'ai_max_history', 'ai_max_retries', 'ai_request_timeout', 'ai_error_message', 'ai_bot_partner_id',
               'ai_bot_name', 'ai_log_payload', 'ai_log_retention_days')
KEPT_SELECTIONS = ('ai_backend_type', 'ai_document_mode')


def check_version(version):
    if parse_version(version) < parse_version(MIN_VERSION):
        raise UserError(
            "im_livechat_ai 18.0.2.0.0: this database has im_livechat_ai %(version)s. Upgrade it first to "
            "18.0.1.3.5 with an image that carries 18.0.1.3.5, then to this version. "
            "（這個資料庫的 im_livechat_ai 是 %(version)s，請先用含 18.0.1.3.5 的映像升到 18.0.1.3.5，再升這一版。）"
            % {'version': version})


def check_bridge(cr):
    cr.execute("SELECT state FROM ir_module_module WHERE name = %s", (BRIDGE,))
    row = cr.fetchone()
    state = row[0] if row else None
    if state in BLOCKING_STATES:
        raise UserError(
            "im_livechat_ai 18.0.2.0.0: the module %(bridge)s is installed on this database (state: %(state)s), "
            "but it is no longer shipped: its views and data would stay without code. Uninstall %(bridge)s with "
            "the previous image first, then run this upgrade again." % {'bridge': BRIDGE, 'state': state})


def _structure(node):
    """Element / attribute tree without display texts; extra text nodes are unexpected."""
    if (not isinstance(node.tag, str)
            or (node.tag != 'div' and (node.text or '').strip())
            or (node.tail or '').strip()):
        raise ValueError('unexpected Settings view XML node or text')
    return [
        node.tag,
        sorted((key, value) for key, value in node.attrib.items() if key not in DISPLAY_ATTRIBUTES),
        [_structure(child) for child in node],
    ]


def has_retired_structure(arch_text):
    """True when every stored language of the view is the 18.0.1.1.0 tree."""
    try:
        arches = json.loads(arch_text)
        if not isinstance(arches, dict) or not arches:
            return False
        for xml in arches.values():
            if not isinstance(xml, str):
                return False
            root = ElementTree.fromstring(xml, parser=ElementTree.XMLParser(
                target=ElementTree.TreeBuilder(insert_comments=True)))
            shape = json.dumps(_structure(root), ensure_ascii=False, separators=(',', ':'))
            if hashlib.sha256(shape.encode()).hexdigest() != LEGACY_STRUCTURE_SHA256:
                return False
    except (TypeError, ValueError, ElementTree.ParseError):
        return False
    return True


def residual_settings_views(cr):
    """Ids of the Settings views that still name a removed livechat_ai_* field (whole word)."""
    pattern = r'(^|[^A-Za-z0-9_])(%s)([^A-Za-z0-9_]|$)' % '|'.join(re.escape(f) for f in REMOVED_SETTINGS_FIELDS)
    cr.execute("SELECT id FROM ir_ui_view WHERE model = %s AND arch_db::text ~ %s ORDER BY id",
               (SETTINGS_MODEL, pattern))
    return [row[0] for row in cr.fetchall()]


def remove_retired_settings_view(cr):
    """Step 3. Returns the id of the view deleted, or None."""
    cr.execute("""
        SELECT d.id, v.id, v.arch_db::text,
               v.name = %s AND v.model = %s
                 AND v.inherit_id = (SELECT base.res_id FROM ir_model_data base
                                      WHERE base.module = 'base' AND base.name = 'res_config_settings_view_form'
                                        AND base.model = 'ir.ui.view'),
               v.arch_updated IS TRUE,
               EXISTS (SELECT 1 FROM ir_ui_view child WHERE child.inherit_id = v.id),
               EXISTS (SELECT 1 FROM ir_model_data alias
                        WHERE alias.model = 'ir.ui.view' AND alias.res_id = v.id AND alias.id != d.id),
               EXISTS (SELECT 1 FROM ir_ui_view_custom custom WHERE custom.ref_id = v.id)
          FROM ir_model_data d
          JOIN ir_ui_view v ON v.id = d.res_id
         WHERE d.module = %s AND d.name = %s AND d.model = 'ir.ui.view'
           FOR UPDATE OF v
    """, (SETTINGS_VIEW_NAME, SETTINGS_MODEL, MODULE, SETTINGS_XMLID))
    target = cr.fetchone()
    removed = None
    if target:
        xmlid_id, view_id, arch_text, is_retired_view, edited, has_children, other_xmlids, customized = target
        if not is_retired_view or not has_retired_structure(arch_text) or edited or has_children \
                or other_xmlids or customized:
            raise UserError(
                "im_livechat_ai 18.0.2.0.0: the retired 18.0.1.1.0 Settings view (ir.ui.view %(view)s, xmlid "
                "im_livechat_ai.%(xmlid)s) is still in the database but it was changed, has a child view, another "
                "xmlid or a customization, so it is not removed automatically. Review and remove it, then run "
                "this upgrade again." % {'view': view_id, 'xmlid': SETTINGS_XMLID})
        cr.execute("DELETE FROM ir_ui_view WHERE id = %s", (view_id,))
        cr.execute("DELETE FROM ir_model_data WHERE id = %s", (xmlid_id,))
        removed = view_id
    residual = residual_settings_views(cr)
    if residual:
        raise UserError(
            "im_livechat_ai 18.0.2.0.0: Settings view(s) %(ids)s still name fields removed by 18.0.1.2.0 "
            "(livechat_ai_*): another module validating the Settings form would fail during this upgrade. "
            "Remove those references, then run this upgrade again." % {'ids': residual})
    return removed


def restore_bridge_xmlids(cr):
    """Step 4. Returns the number of xmlids moved back."""
    restored = 0
    for name, sources in BRIDGE_XMLIDS:
        cr.execute("SELECT 1 FROM ir_model_data WHERE module = %s AND name = %s", (MODULE, name))
        if cr.fetchone():
            continue
        for module, old_name in sources:
            cr.execute("""
                UPDATE ir_model_data SET module = %s, name = %s, noupdate = TRUE
                 WHERE id = (SELECT id FROM ir_model_data WHERE module = %s AND name = %s ORDER BY id LIMIT 1)
            """, (MODULE, name, module, old_name))
            if cr.rowcount:
                restored += 1
                break
    return restored


def keep_moved_columns(cr):
    """Step 5. Returns the number of reflections made noupdate."""
    names = ['field_im_livechat_channel__%s' % field for field in KEPT_FIELDS]
    patterns = ['selection__im_livechat_channel__%s__%%' % field for field in KEPT_SELECTIONS]
    cr.execute("""
        UPDATE ir_model_data SET noupdate = TRUE
         WHERE module = %s AND COALESCE(noupdate, FALSE) = FALSE
           AND ((model = 'ir.model.fields' AND name IN %s)
                OR (model = 'ir.model.fields.selection' AND name LIKE ANY(%s)))
    """, (MODULE, tuple(names), patterns))
    return cr.rowcount


def migrate(cr, version):
    if not version:
        return
    check_version(version)
    check_bridge(cr)
    removed = remove_retired_settings_view(cr)
    restored = restore_bridge_xmlids(cr)
    kept = keep_moved_columns(cr)
    _logger.info("im_livechat_ai 18.0.2.0.0 pre-migrate (from %s): retired Settings view removed: %s; bot "
                 "xmlid(s) restored: %s; channel column reflection(s) kept for a rollback: %s",
                 version, removed or 'none', restored, kept)
