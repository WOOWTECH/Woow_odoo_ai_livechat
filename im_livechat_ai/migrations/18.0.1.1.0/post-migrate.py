# -*- coding: utf-8 -*-
"""18.0.1.1.0 post-migration.

- Drop the legacy credential columns if they are still there (values were
  already wiped by pre-migrate).
- Turn AI off on every channel: an administrator must configure LiteLLM in
  Settings > Live Chat AI and re-enable each channel.
- Remove conversation content and upstream response bodies from existing
  API logs ("Log Conversation Content" is off by default).
- Package C: drop ai_retry_delay (fixed 3 s / 10 s backoff now) and convert
  ai_max_retries: it used to count total calls (1-10, empty = 3), it now
  counts retries after the first call (0-2), so the value becomes
  calls - 1, capped to 2 (1 -> 0, 2 -> 1, 3 or more -> 2).
- ai_max_tokens is capped to 8192.
- Package D: channels still using the old English default error message
  (or none) get the Traditional Chinese default (P2-7); existing AI bot
  partners are archived and tagged "AI 機器人" (P2-6). A linked "bot"
  partner that has a user (a real person) is unlinked instead, so it is
  never renamed or used as the AI author; the channel gets a new bot when
  AI is enabled again.
- The old English default bot name "AI Assistant" becomes "AI 客服助理"
  (channels and their bot partners, and the module's default bot).
- P0-1: channels still using the earlier default handoff keywords (or none)
  get the new default (explicit requests such as 找真人 / talk to a human);
  customised keywords stay.
- P1-7: AI bot partners without an image get the module's bot avatar.

Idempotency (P2-4): every step that changes data (turning AI off,
converting ai_max_retries, scrubbing the API logs, the error message, the
bot name, unlinking/archiving/tagging the bot partners, the handoff
keywords, the bot avatars) only runs when pre-migrate saw the 18.0.1.0.x
schema (flag im_livechat_ai.migration_1_1_0_legacy, deleted here). Without
it only leftover legacy columns are dropped and ai_max_tokens above 8192
(which the model refuses) is capped. So running the 18.0.1.1.0 scripts
again on a migrated database changes nothing, also not values an
administrator cleared or changed afterwards (an emptied bot name or error
message, an unarchived or untagged bot partner, a removed avatar, emptied
keywords).
"""

import base64
import logging

from odoo import SUPERUSER_ID, api, tools
from odoo.tools import SQL, sql

_logger = logging.getLogger(__name__)

TABLE = 'im_livechat_channel'
LEGACY_COLUMNS = ('ai_api_key', 'ai_api_base_url')
UNUSED_COLUMNS = ('ai_retry_delay',)
MAX_RETRIES = 2
MAX_TOKENS = 8192
LEGACY_BOT_NAME = 'AI Assistant'
NEW_BOT_NAME = 'AI 客服助理'
LOG_TABLE = 'llm_api_log'
MAX_LOG_ERROR_LENGTH = 300
LEGACY_FLAG_KEY = 'im_livechat_ai.migration_1_1_0_legacy'
LEGACY_HANDOFF_KEYWORDS = '真人,客服,人工,真人客服,human,agent,operator'
NEW_HANDOFF_KEYWORDS = (
    '找真人,真人客服,轉真人,轉人工,人工客服,找客服,聯絡客服,我要客服,'
    'human agent,talk to a human,real person,live agent'
)
BOT_AVATAR_PATH = 'im_livechat_ai/static/img/ai_bot.png'


def pop_legacy_flag(cr):
    """Read and delete the flag set by pre-migrate (True: 18.0.1.0.x data)."""
    cr.execute("DELETE FROM ir_config_parameter WHERE key = %s RETURNING value", (LEGACY_FLAG_KEY,))
    return any(row[0] == '1' for row in cr.fetchall())


def drop_legacy_columns(cr):
    dropped = []
    for column in LEGACY_COLUMNS:
        if sql.column_exists(cr, TABLE, column):
            # Wipe again in case anything wrote between pre and post.
            cr.execute(SQL("UPDATE %s SET %s = NULL", SQL.identifier(TABLE), SQL.identifier(column)))
            cr.execute(SQL("ALTER TABLE %s DROP COLUMN %s", SQL.identifier(TABLE), SQL.identifier(column)))
            dropped.append(column)
    return dropped


def drop_unused_columns(cr):
    dropped = []
    for column in UNUSED_COLUMNS:
        if sql.column_exists(cr, TABLE, column):
            cr.execute(SQL("ALTER TABLE %s DROP COLUMN %s", SQL.identifier(TABLE), SQL.identifier(column)))
            dropped.append(column)
    return dropped


def convert_max_retries(cr):
    """Total calls (old meaning, empty/0 = 3) -> retries after the first call."""
    if not sql.column_exists(cr, TABLE, 'ai_max_retries'):
        return 0
    cr.execute(SQL(
        "UPDATE %s SET ai_max_retries = LEAST(GREATEST(COALESCE(NULLIF(ai_max_retries, 0), 3) - 1, 0), %s)",
        SQL.identifier(TABLE), MAX_RETRIES,
    ))
    return cr.rowcount


def cap_max_tokens(cr):
    if not sql.column_exists(cr, TABLE, 'ai_max_tokens'):
        return 0
    cr.execute(SQL(
        "UPDATE %s SET ai_max_tokens = %s WHERE ai_max_tokens > %s",
        SQL.identifier(TABLE), MAX_TOKENS, MAX_TOKENS,
    ))
    return cr.rowcount


def disable_all_channels(cr):
    cr.execute(SQL(
        "UPDATE %s SET ai_enabled = FALSE WHERE ai_enabled IS DISTINCT FROM FALSE RETURNING id",
        SQL.identifier(TABLE),
    ))
    return [row[0] for row in cr.fetchall()]


def scrub_logs(cr):
    if not sql.table_exists(cr, LOG_TABLE):
        return 0
    cr.execute(SQL(
        """UPDATE %s
              SET request_payload = NULL,
                  response_payload = NULL,
                  error_message = LEFT(split_part(error_message, ' | Response:', 1), %s)
            WHERE request_payload IS NOT NULL
               OR response_payload IS NOT NULL
               OR error_message LIKE %s
               OR length(error_message) > %s""",
        SQL.identifier(LOG_TABLE), MAX_LOG_ERROR_LENGTH, '%| Response:%', MAX_LOG_ERROR_LENGTH,
    ))
    return cr.rowcount


# Kept literal here (not imported) so the migration keeps working if the
# model constants change later.
LEGACY_ERROR_MESSAGE = (
    'Sorry, the AI assistant is temporarily unavailable. '
    'Please try again later or leave your contact information.'
)
NEW_ERROR_MESSAGE = (
    '抱歉，AI 客服暫時無法回覆。請稍後再試，或留下您的聯絡方式，'
    '我們的客服人員會盡快與您聯繫。'
)


def translate_error_message(cr):
    """Old English default (or empty) error message -> Chinese default."""
    if not sql.column_exists(cr, TABLE, 'ai_error_message'):
        return []
    cr.execute(SQL(
        """UPDATE %s SET ai_error_message = %s
            WHERE ai_error_message IS NULL
               OR btrim(ai_error_message) = ''
               OR btrim(ai_error_message) = %s
        RETURNING id""",
        SQL.identifier(TABLE), NEW_ERROR_MESSAGE, LEGACY_ERROR_MESSAGE,
    ))
    return [row[0] for row in cr.fetchall()]


def unlink_user_bot_partners(cr):
    """Channels whose bot partner has a user (a real person): unlink it."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    channels = env['im_livechat.channel'].with_context(active_test=False).search([
        ('ai_bot_partner_id', '!=', False),
    ])
    wrong = channels.filtered(
        lambda c: c.ai_bot_partner_id.with_context(active_test=False).user_ids
    )
    if wrong:
        wrong.write({'ai_bot_partner_id': False})
    return wrong.ids


def rename_legacy_bot_name(cr):
    """Old English default bot name -> Chinese default (channels, their bot
    partners and the module's default bot)."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    channels = env['im_livechat.channel'].with_context(active_test=False).search([
        '|', ('ai_bot_name', '=', LEGACY_BOT_NAME), ('ai_bot_name', '=', False),
    ])
    if channels:
        channels.write({'ai_bot_name': NEW_BOT_NAME})
    partners = channels.ai_bot_partner_id
    default_bot = env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False)
    if default_bot:
        partners |= default_bot
    partners = partners.with_context(active_test=False).filtered(
        lambda p: p.name == LEGACY_BOT_NAME and not p.user_ids
    )
    if partners:
        partners.write({'name': NEW_BOT_NAME})
    return channels.ids


def migrate_handoff_keywords(cr):
    """Earlier default (or no) handoff keywords -> new default (P0-1)."""
    if not sql.column_exists(cr, TABLE, 'ai_handoff_keywords'):
        return []
    cr.execute(SQL(
        """UPDATE %s SET ai_handoff_keywords = %s
            WHERE ai_handoff_keywords IS NULL
               OR btrim(ai_handoff_keywords) = ''
               OR replace(ai_handoff_keywords, ' ', '') = %s
        RETURNING id""",
        SQL.identifier(TABLE), NEW_HANDOFF_KEYWORDS, LEGACY_HANDOFF_KEYWORDS,
    ))
    return [row[0] for row in cr.fetchall()]


def set_bot_avatars(cr):
    """AI bot partners (channel bots and the default bot) without an image
    get the module's avatar (P1-7). Partners with a user are never touched."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    partners = env['im_livechat.channel'].with_context(active_test=False).search([]).ai_bot_partner_id
    default_bot = env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False)
    if default_bot:
        partners |= default_bot
    partners = partners.with_context(active_test=False).filtered(
        lambda p: not p.user_ids and not p.image_1920
    )
    if not partners:
        return 0
    with tools.file_open(BOT_AVATAR_PATH, 'rb') as avatar:
        image = base64.b64encode(avatar.read())
    partners.write({'image_1920': image})
    return len(partners)


def tag_bot_partners(cr):
    """Archive the existing AI bot partners and tag them "AI 機器人"."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    category = env.ref('im_livechat_ai.partner_category_ai_bot', raise_if_not_found=False)
    partners = env['im_livechat.channel'].with_context(active_test=False).search([]).ai_bot_partner_id
    default_bot = env.ref('im_livechat_ai.partner_ai_bot', raise_if_not_found=False)
    if default_bot:
        partners |= default_bot
    # Never archive or tag a real person.
    partners = partners.with_context(active_test=False).filtered(lambda p: not p.user_ids)
    if not partners:
        return 0
    vals = {'active': False}
    if category:
        vals['category_id'] = [(4, category.id)]
    partners.with_context(active_test=False).write(vals)
    return len(partners)


def migrate(cr, version):
    if not version:
        return
    legacy = pop_legacy_flag(cr)
    dropped = drop_legacy_columns(cr)
    if dropped:
        _logger.info("im_livechat_ai: dropped legacy columns %s.%s", TABLE, dropped)
    unused = drop_unused_columns(cr)
    if unused:
        _logger.info("im_livechat_ai: dropped unused columns %s.%s", TABLE, unused)
    capped = cap_max_tokens(cr)
    if capped:
        _logger.info("im_livechat_ai: capped ai_max_tokens to %s on %s channel(s)", MAX_TOKENS, capped)
    if not legacy:
        _logger.info("im_livechat_ai 18.0.1.1.0 post-migrate: no 18.0.1.0.x schema found; "
                     "channel settings, bot partners and API logs left as they are")
        return
    migrate_legacy_data(cr)


def migrate_legacy_data(cr):
    """The data conversions of an upgrade from 18.0.1.0.x (run once, gated
    on the pre-migrate flag)."""
    disabled = disable_all_channels(cr)
    _logger.warning(
        "im_livechat_ai 18.0.1.1.0: AI replies were turned OFF on all live chat channels "
        "(%s channel(s) had AI on: %s). The per-channel API URL/key were removed. "
        "A system administrator must set the LiteLLM URL and API key in Settings > Live Chat AI, "
        "click 'Fetch Available Models', then re-enable AI on each channel.",
        len(disabled), disabled,
    )
    converted = convert_max_retries(cr)
    if converted:
        _logger.info("im_livechat_ai: converted ai_max_retries (total calls -> retries) on %s channel(s)",
                     converted)
    scrubbed = scrub_logs(cr)
    if scrubbed:
        _logger.info("im_livechat_ai: removed conversation content/response bodies from %s API log(s)",
                     scrubbed)
    translated = translate_error_message(cr)
    if translated:
        _logger.info("im_livechat_ai: error message set to the Chinese default on channel(s) %s", translated)
    unlinked = unlink_user_bot_partners(cr)
    if unlinked:
        _logger.warning("im_livechat_ai: channel(s) %s used a partner with a user as AI bot; unlinked it",
                        unlinked)
    renamed = rename_legacy_bot_name(cr)
    if renamed:
        _logger.info("im_livechat_ai: bot name 'AI Assistant' -> '%s' on channel(s) %s", NEW_BOT_NAME, renamed)
    tagged = tag_bot_partners(cr)
    if tagged:
        _logger.info("im_livechat_ai: archived and tagged %s AI bot partner(s)", tagged)
    keywords = migrate_handoff_keywords(cr)
    if keywords:
        _logger.info("im_livechat_ai: handoff keywords set to the new default on channel(s) %s", keywords)
    avatars = set_bot_avatars(cr)
    if avatars:
        _logger.info("im_livechat_ai: bot avatar set on %s AI bot partner(s)", avatars)
