# -*- coding: utf-8 -*-

from . import controllers
from . import models


def uninstall_hook(env):
    """The access tokens live in an SQL-only column that Odoo does not know
    as a field: drop it, so no token stays in the database."""
    env.cr.execute("ALTER TABLE im_livechat_channel DROP COLUMN IF EXISTS ai_api_token_secret")
