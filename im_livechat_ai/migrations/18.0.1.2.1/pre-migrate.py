"""Remove only the retired 1.1.0 Settings view before Odoo validates views.

The 1.2.0 source removed its Python settings fields and XML data file. Odoo
normally removes retired XML records at the end of the module load, which is
too late for the inherited view that still names livechat_ai_base_url.
"""

import logging

_logger = logging.getLogger(__name__)

MODULE = 'im_livechat_ai'
XMLID = 'res_config_settings_view_form_livechat_ai'
VIEW_NAME = 'res.config.settings.view.form.inherit.im_livechat_ai'
VIEW_MODEL = 'res.config.settings'
LEGACY_FIELD = '%livechat_ai_base_url%'


def migrate(cr, version):
    if not version:  # new installation has no legacy record
        return

    cr.execute('''
        SELECT d.id, v.id,
               v.name = %s AND v.model = %s
                 AND v.inherit_id = (
                     SELECT base.res_id FROM ir_model_data AS base
                      WHERE base.module = 'base'
                        AND base.name = 'res_config_settings_view_form'
                        AND base.model = 'ir.ui.view'
                 )
                 AND CAST(v.arch_db AS TEXT) LIKE %s AS is_legacy_view,
               EXISTS (SELECT 1 FROM ir_ui_view AS child WHERE child.inherit_id = v.id),
               EXISTS (SELECT 1 FROM ir_model_data AS alias
                        WHERE alias.model = 'ir.ui.view'
                          AND alias.res_id = v.id AND alias.id != d.id)
          FROM ir_model_data AS d
          JOIN ir_ui_view AS v ON v.id = d.res_id
         WHERE d.module = %s AND d.name = %s AND d.model = 'ir.ui.view'
    ''', (VIEW_NAME, VIEW_MODEL, LEGACY_FIELD, MODULE, XMLID))
    target = cr.fetchone()
    if target is None:
        return

    xmlid_id, view_id, is_legacy_view, has_children, has_other_xmlids = target
    if not is_legacy_view:
        _logger.warning('im_livechat_ai: retired Settings XMLID has a different view shape; leaving it intact')
        return
    if has_children or has_other_xmlids:
        raise RuntimeError('im_livechat_ai: retired Settings view has dependants; manual review required')

    cr.execute('''
        DELETE FROM ir_ui_view AS v
         WHERE v.id = %s AND v.name = %s AND v.model = %s
           AND v.inherit_id = (
               SELECT base.res_id FROM ir_model_data AS base
                WHERE base.module = 'base'
                  AND base.name = 'res_config_settings_view_form'
                  AND base.model = 'ir.ui.view'
           )
           AND CAST(v.arch_db AS TEXT) LIKE %s
           AND EXISTS (SELECT 1 FROM ir_model_data AS d
                        WHERE d.id = %s AND d.module = %s AND d.name = %s
                          AND d.model = 'ir.ui.view' AND d.res_id = v.id)
           AND NOT EXISTS (SELECT 1 FROM ir_ui_view AS child WHERE child.inherit_id = v.id)
           AND NOT EXISTS (SELECT 1 FROM ir_model_data AS alias
                            WHERE alias.model = 'ir.ui.view' AND alias.res_id = v.id
                              AND alias.id != %s)
    ''', (view_id, VIEW_NAME, VIEW_MODEL, LEGACY_FIELD, xmlid_id, MODULE, XMLID, xmlid_id))
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
