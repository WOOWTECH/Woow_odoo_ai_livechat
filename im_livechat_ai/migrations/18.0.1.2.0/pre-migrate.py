# -*- coding: utf-8 -*-
"""18.0.1.2.0 pre-migration: create the ai_document_mode column empty.

Without it the registry would add the column with its default ('file') on
every channel, and post-migrate could no longer tell which channels it has
to decide. post-migrate fills every empty value: 'link' for Hermes channels
(model 'hermes-agent' or an endpoint URL containing 'hermes'), 'file' for
the others; a value already set is never changed.
"""

from odoo.tools import SQL, sql

TABLE = 'im_livechat_channel'
COLUMN = 'ai_document_mode'


def migrate(cr, version):
    if not version or not sql.table_exists(cr, TABLE):
        return
    if not sql.column_exists(cr, TABLE, COLUMN):
        cr.execute(SQL("ALTER TABLE %s ADD COLUMN %s VARCHAR", SQL.identifier(TABLE), SQL.identifier(COLUMN)))
