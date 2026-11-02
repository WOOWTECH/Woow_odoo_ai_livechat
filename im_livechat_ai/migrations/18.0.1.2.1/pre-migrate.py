# -*- coding: utf-8 -*-
"""18.0.1.2.1 pre-migration: delete the 18.0.1.1.0 Settings view before the module loads.

18.0.1.2.0 moved every setting to the Live Chat channel and removed the Settings
page, but the database still holds the old view
`im_livechat_ai.res_config_settings_view_form_livechat_ai`. Odoo removes records
whose file is gone only after all modules have loaded, and it commits module by
module. So during the upgrade that old view, which names the removed
`livechat_ai_*` fields, is part of the res.config.settings form of every module
loaded later; the first of them that validates its settings view fails and the
whole upgrade stops (production platform, 2026-11-02).

Deleting the view (and any view that inherits from it) up front lets the upgrade
run. The removed menu and window action are harmless and are left to Odoo's
normal cleanup.
"""

import logging

_logger = logging.getLogger(__name__)

REMOVED_VIEWS = ('res_config_settings_view_form_livechat_ai',)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'im_livechat_ai' AND model = 'ir.ui.view' AND name IN %s
    """, (REMOVED_VIEWS,))
    roots = [row[0] for row in cr.fetchall()]
    if not roots:
        return
    cr.execute("""
        WITH RECURSIVE tree(id) AS (
            SELECT id FROM ir_ui_view WHERE id IN %s
            UNION
            SELECT v.id FROM ir_ui_view v JOIN tree t ON v.inherit_id = t.id
        )
        SELECT id FROM tree
    """, (tuple(roots),))
    view_ids = tuple(row[0] for row in cr.fetchall())
    if view_ids:
        cr.execute("DELETE FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id IN %s", (view_ids,))
        cr.execute("DELETE FROM ir_ui_view WHERE id IN %s", (view_ids,))
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'im_livechat_ai' AND model = 'ir.ui.view' AND name IN %s
    """, (REMOVED_VIEWS,))
    _logger.info("im_livechat_ai 18.0.1.2.1 pre-migrate: removed %d legacy Settings view(s) %s",
                 len(view_ids), list(view_ids))
