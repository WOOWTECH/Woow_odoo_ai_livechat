# -*- coding: utf-8 -*-

import base64
import hashlib
import hmac
import html as html_lib
import io
import logging
import os.path
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.tools.mimetypes import guess_mimetype

_logger = logging.getLogger(__name__)

# Regex to strip HTML tags for building clean LLM context
_HTML_TAG_RE = re.compile(r'<[^>]+>')

# Prefix of a human operator's message in the LLM history (P1-3).
OPERATOR_PREFIX = '[真人客服 %s] '
# Added to the system prompt when the history contains operator messages
# (operator messages are user turns, as in woow_ai_contact).
OPERATOR_SYSTEM_NOTE = (
    '對話中以「[真人客服 姓名]」開頭的訊息是真人客服同事對訪客說的話，不是訪客說的，也不是你說的；'
    '不要推翻、否認或更改同事的說法與承諾，必要時請訪客等候同事處理。'
)
# Added to the system prompt of a reply sent when a handoff wait ended
# without an operator (auto-resume, P0-1).
RESUME_SYSTEM_NOTE = (
    '訪客先前要求真人客服，但客服人員在等候時間內沒有回覆，現在由你繼續服務。'
    '對話中「已幫您通知客服」「已通知客服人員」這類訊息是系統自動送出的等候通知，不是回答；'
    '請一併回答訪客在等候期間提出、還沒有被回答的所有問題。'
)

# Owner decision 40 (same as woow_ai_contact_livechat): the backend decides
# when the visitor goes to a human. openai backends (LiteLLM, Hermes) end
# their reply with this marker; n8n answers "handoff": true or uses the
# marker in "output" (Chat Trigger workflows).
HANDOFF_MARKER = '[[handoff]]'
# case insensitive, spaces inside allowed, also escaped (\[\[handoff\]\]) or in backticks
HANDOFF_MARKER_RE = re.compile(r'`?\\?\[\\?\[\s*handoff\s*\\?\]\\?\]`?', re.IGNORECASE)
# The only prompt text Odoo adds for the handoff (openai backends, channels with operators).
HANDOFF_SYSTEM_LINE = (
    '訪客要求真人客服、想跟真人說話時，在回覆最後單獨一行寫 [[handoff]]，系統就會轉接真人客服（不要說無法轉接；'
    '只問電話或聯絡方式不算）。When the visitor asks for a human, end your reply with a line [[handoff]] '
    'and Odoo hands the conversation to a human operator.'
)


def strip_handoff_marker(text):
    """(text without the [[handoff]] markers, whether one was found)."""
    if not text or not HANDOFF_MARKER_RE.search(text):
        return text, False
    text = HANDOFF_MARKER_RE.sub('', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip(), True


# Why the AI of a session is paused (discuss.channel.ai_pause_reason)
PAUSE_OPERATOR = 'operator'
PAUSE_HANDOFF = 'handoff'

# Author kinds returned by _ai_author_kind
AUTHOR_VISITOR = 'visitor'
AUTHOR_OPERATOR = 'operator'  # internal (non-share) user who is not the visitor: a human operator
AUTHOR_BOT = 'bot'  # AI bot, OdooBot, or a session operator without internal user (chatbot)


# --- Visitor attachments (owner decision 39, same limits as woow_ai_contact) ---
# Visitor images and files go to the backend as they are: no text extraction
# in Odoo. Only attachments of visitor messages are used, never those of the
# bot, the operators or OdooBot.
DOCUMENT_MODE_FILE = 'file'  # documents as OpenAI ``file`` parts (LiteLLM, OpenAI)
DOCUMENT_MODE_LINK = 'link'  # documents as signed download links (Hermes...)
# Inline image types (detected from the content, never from the name).
INLINE_IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
INLINE_IMAGE_MESSAGES = 3  # the most recent visitor messages whose images are inlined
INLINE_IMAGE_MAX_COUNT = 4  # images inlined per request
INLINE_DOC_MESSAGES = 5  # the most recent visitor messages whose documents are inlined ('file' mode)
INLINE_DOC_MAX_COUNT = 5  # documents inlined per request
INLINE_FILE_MAX_BYTES = 5 * 1024 * 1024  # per inlined image or document
# data: URI characters per request, images and documents together (Hermes
# rejects requests over 10 MB).
INLINE_DATA_BUDGET = 7 * 1024 * 1024
# Signed download URL of a visitor attachment (both backends).
SIGNED_URL_TTL = 3600  # seconds
SIGN_SCOPE = 'im_livechat_ai'
ATTACHMENT_ROUTE = '/im_livechat_ai/attachment'

# Active content (a browser renders or runs it): always a link, never inlined.
# Exact names plus any "+xml" type: a substring test on "xml" would also hit
# the OOXML office types (xlsx, docx, pptx).
ACTIVE_TYPES = frozenset((
    'text/html', 'application/xhtml+xml', 'image/svg+xml', 'text/xml', 'application/xml',
    'text/javascript', 'application/javascript', 'application/x-javascript',
    'text/ecmascript', 'application/ecmascript', 'message/rfc822', 'multipart/related',
))
EXECUTABLE_TYPES = frozenset((
    'application/x-msdownload', 'application/x-dosexec', 'application/x-executable',
    'application/x-elf', 'application/x-mach-binary', 'application/x-sharedlib',
    'application/vnd.microsoft.portable-executable', 'application/x-msi',
    'application/x-ms-installer', 'application/x-ms-shortcut', 'application/x-bat',
    'application/x-sh', 'application/x-shellscript', 'text/x-shellscript', 'text/x-sh',
    'application/x-csh', 'application/x-python', 'text/x-python', 'application/x-perl',
    'text/x-perl', 'application/x-php', 'text/x-php', 'application/x-ruby', 'text/x-ruby',
    'application/x-powershell', 'application/hta', 'application/java-archive',
    'application/x-java-archive', 'application/vnd.android.package-archive',
    'application/x-apple-diskimage', 'application/x-debian-package', 'application/x-rpm',
))
ACTIVE_EXTENSIONS = frozenset((
    'html', 'htm', 'xhtml', 'shtml', 'mht', 'mhtml', 'svg', 'svgz', 'xml', 'xsl', 'xslt',
))
EXECUTABLE_EXTENSIONS = frozenset((
    'exe', 'dll', 'msi', 'msp', 'com', 'scr', 'pif', 'cpl', 'bat', 'cmd', 'ps1', 'psm1',
    'vbs', 'vbe', 'wsf', 'wsh', 'hta', 'lnk', 'reg', 'js', 'mjs', 'cjs', 'jse', 'jar',
    'apk', 'app', 'dmg', 'pkg', 'deb', 'rpm', 'sh', 'bash', 'zsh', 'csh', 'ksh', 'command',
    'py', 'pyc', 'pyw', 'pl', 'rb', 'php', 'phtml', 'elf', 'run', 'so',
))
ZIP_TYPES = frozenset(('application/zip', 'application/x-zip-compressed', 'application/x-zip'))
# Archives Odoo cannot look into: whether they carry executables is unknown,
# so they are links only.
OPAQUE_ARCHIVE_TYPES = frozenset((
    'application/x-rar-compressed', 'application/vnd.rar', 'application/x-rar',
    'application/x-7z-compressed', 'application/x-tar', 'application/gzip', 'application/x-gzip',
    'application/x-bzip2', 'application/x-xz', 'application/vnd.ms-cab-compressed',
    'application/x-compress', 'application/zstd',
))
OPAQUE_ARCHIVE_EXTENSIONS = frozenset(('rar', '7z', 'tar', 'gz', 'tgz', 'bz2', 'tbz2', 'xz', 'txz', 'cab',
                                       'z', 'zst', 'lz', 'lzma'))
ZIP_EXTENSIONS = frozenset(('zip',))
# Documents sent as ``file`` parts in 'file' mode (same list as
# woow_ai_contact): the types LiteLLM / OpenAI read. Any other file (archive,
# audio, video...) is a link only, so one unsupported part never makes the
# backend refuse the whole request. Extension -> mimetype used when the
# uploaded one is missing.
OOXML_DOCUMENT_TYPES = {
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'xlsm': 'application/vnd.ms-excel.sheet.macroEnabled.12',
}
TEXT_DOCUMENT_TYPES = {
    'txt': 'text/plain', 'text': 'text/plain', 'log': 'text/plain', 'md': 'text/markdown',
    'markdown': 'text/markdown', 'csv': 'text/csv', 'tsv': 'text/tab-separated-values',
    'json': 'application/json',
}
DOCUMENT_TYPES = {'pdf': 'application/pdf', 'xls': 'application/vnd.ms-excel',
                  **OOXML_DOCUMENT_TYPES, **TEXT_DOCUMENT_TYPES}
ZIP_LIKE_TYPES = ('application/zip', 'application/x-zip-compressed', 'application/octet-stream')
OLE_TYPES = ('application/vnd.ms-excel', 'application/x-ole-storage', 'application/cdfv2',
             'application/octet-stream')
TEXT_LIKE_TYPES = ('application/json', 'application/csv', 'application/x-ndjson')

ATTACHMENT_LINE = '[附件] %s（%s，%s）：%s'
ATTACHED_TEXT = '檔案已附在這則訊息'
ATTACHMENT_NOTE = '[附件說明] %s：%s'

_DATA_URI_RE = re.compile(r'(data:[a-z0-9.+-]+/[a-z0-9.+-]+(?:;[a-z0-9.+=-]+)*;base64),([A-Za-z0-9+/]+=*)',
                          re.IGNORECASE)


def human_size(size):
    """1234567 -> '1.2 MB' (same format as woow_ai_contact)."""
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024
    return '%d B' % size


def file_extension(name):
    return os.path.splitext(name or '')[1].lower().lstrip('.')


def plain_mimetype(mimetype):
    return (mimetype or '').split(';')[0].strip().lower()


def sniff_mimetype(data):
    """Mimetype guessed from the content only (lowercase, no parameters)."""
    return plain_mimetype(guess_mimetype(data or b'', default='application/octet-stream')) \
        or 'application/octet-stream'


def is_active_type(mimetype):
    mimetype = plain_mimetype(mimetype)
    return (mimetype in ACTIVE_TYPES or mimetype.endswith('+xml') or 'javascript' in mimetype
            or 'ecmascript' in mimetype)


def _zip_has_executable(raw):
    """True when a zip archive carries an executable or a script (or cannot be read)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, NotImplementedError):
        return True
    return any(file_extension(name) in EXECUTABLE_EXTENSIONS for name in names if not name.endswith('/'))


def is_link_only(name, mimetype, raw=None):
    """True for dangerous or active files, which are never inlined: html,
    svg, xml, javascript, executables and scripts, archives with executables
    (and archives Odoo cannot look into). Checks the name, the uploaded
    mimetype and, when ``raw`` is given, the content."""
    extension = file_extension(name)
    if extension in ACTIVE_EXTENSIONS or extension in EXECUTABLE_EXTENSIONS \
            or extension in OPAQUE_ARCHIVE_EXTENSIONS:
        return True
    declared = plain_mimetype(mimetype)
    if is_active_type(declared) or declared in EXECUTABLE_TYPES or declared in OPAQUE_ARCHIVE_TYPES:
        return True
    if raw is None:
        return False
    sniffed = sniff_mimetype(raw)
    if is_active_type(sniffed) or sniffed in EXECUTABLE_TYPES or sniffed in OPAQUE_ARCHIVE_TYPES:
        return True
    if raw[:2] == b'MZ' or raw[:4] == b'\x7fELF' or raw[:2] == b'#!':
        return True  # Windows / ELF executable, script with a shebang
    if extension in ZIP_EXTENSIONS or declared in ZIP_TYPES or sniffed in ZIP_TYPES or raw[:4] == b'PK\x03\x04':
        return _zip_has_executable(raw)  # also the OOXML documents (zip files)
    return False


def document_mimetype(name, mimetype, raw):
    """Mimetype a visitor document is sent with as a ``file`` part (the
    uploaded one, unchanged), or None when the file is not a document of
    DOCUMENT_TYPES whose content matches its kind (a link only then)."""
    extension = file_extension(name)
    declared = plain_mimetype(mimetype)
    if not raw or (extension not in DOCUMENT_TYPES and declared != 'application/pdf'):
        return None
    sniffed = sniff_mimetype(raw)
    if sniffed == 'application/pdf':
        kind_ok = extension == 'pdf' or declared == 'application/pdf'
    elif extension in OOXML_DOCUMENT_TYPES:
        kind_ok = raw[:4] == b'PK\x03\x04' and (sniffed in ZIP_LIKE_TYPES or sniffed in OOXML_DOCUMENT_TYPES.values())
    elif extension == 'xls':
        kind_ok = raw[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' and sniffed in OLE_TYPES
    elif extension in TEXT_DOCUMENT_TYPES:
        kind_ok = sniffed.startswith('text/') or sniffed in TEXT_LIKE_TYPES
    else:
        kind_ok = False
    if not kind_ok:
        return None
    if declared and declared != 'application/octet-stream':
        return mimetype
    return DOCUMENT_TYPES.get(extension) or 'application/pdf'


def data_uri_length(mimetype, size):
    return len('data:%s;base64,' % mimetype) + 4 * ((size + 2) // 3)


def make_data_uri(mimetype, raw):
    return 'data:%s;base64,%s' % (mimetype, base64.b64encode(raw).decode())


def _signature(secret, attachment_id, checksum, expires):
    message = '%s|%s|%s|%s' % (SIGN_SCOPE, attachment_id, checksum or '', expires)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def make_token(secret, attachment_id, checksum, now=None, ttl=SIGNED_URL_TTL):
    """'<expires>.<hex HMAC-SHA256(secret, "im_livechat_ai|<id>|<checksum>|<expires>")>', expires."""
    expires = int(now if now is not None else time.time()) + ttl
    return '%s.%s' % (expires, _signature(secret, attachment_id, checksum, expires)), expires


def check_token(secret, attachment_id, checksum, token, now=None):
    """True when the token was signed for this attachment and has not expired."""
    if not secret or not token or '.' not in token:
        return False
    expires, signature = token.split('.', 1)
    if not expires.isdigit() or len(expires) > 12:
        return False
    if int(expires) < int(now if now is not None else time.time()):
        return False
    return hmac.compare_digest(_signature(secret, attachment_id, checksum, int(expires)), signature)


def iso_utc(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def redact_base64(data):
    """Copy of a request / response payload for the logs: the base64 part of
    every data: URI becomes ``<N bytes base64 omitted>`` (N = its length)."""
    if isinstance(data, str):
        return _DATA_URI_RE.sub(lambda m: '%s,<%d bytes base64 omitted>' % (m.group(1), len(m.group(2))), data)
    if isinstance(data, dict):
        return {key: redact_base64(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact_base64(value) for value in data]
    return data


def attachment_raw(attachment):
    """Content of one attachment. ``raw`` is computed: read through the
    history's shared prefetch set it would load every file at once."""
    return attachment.with_prefetch(attachment._ids).raw or b''


def _clean_text(body):
    """Message HTML -> plain text."""
    return html_lib.unescape(_HTML_TAG_RE.sub('', body or '')).strip()


# Leading user turn that quotes assistant messages which would otherwise
# start the conversation (same text as woow_ai_contact).
EARLIER_ASSISTANT_PREFIX = '[Messages you (the assistant) sent before this user message]\n'


def normalize_turns(turns):
    """OpenAI chat turns (no system message) that start and end with a user turn.

    Hermes treats the last item as the new input, and Anthropic-style models
    would continue a trailing assistant turn or reject a leading one:

    - assistant turns before the first user turn (the welcome message) are dropped;
    - assistant turns after the last user turn (error or notice messages,
      operator replies) are moved just before it;
    - if that leaves assistant turns first (only one user turn), they become
      one user turn quoting them, so their content is not lost.
    """
    turns = list(turns)
    while turns and turns[0]['role'] != 'user':
        turns.pop(0)
    last_user = max((i for i, turn in enumerate(turns) if turn['role'] == 'user'), default=None)
    if last_user is None:
        return turns
    if last_user < len(turns) - 1:
        turns = turns[:last_user] + turns[last_user + 1:] + [turns[last_user]]
    lead = 0
    while turns[lead]['role'] != 'user':
        lead += 1
    if lead:
        quoted = '\n\n'.join(turn['content'] or '' for turn in turns[:lead])
        turns = [{'role': 'user', 'content': EARLIER_ASSISTANT_PREFIX + quoted}] + turns[lead:]
    return turns


class DiscussChannel(models.Model):
    """
    Extend discuss.channel to intercept livechat messages and trigger AI responses.

    This extension hooks into the message notification pipeline to queue AI
    replies (sent to the channel's backend) when visitors write in live chat
    sessions.
    """
    _inherit = 'discuss.channel'

    ai_paused_until = fields.Datetime(
        string='AI Paused Until',
        readonly=True,
        copy=False,
        help='The AI does not answer this live chat conversation before this '
             'time: an operator replied or the visitor asked for a human.',
    )
    ai_pause_reason = fields.Selection(
        selection=[
            (PAUSE_OPERATOR, 'Operator replied'),
            (PAUSE_HANDOFF, 'Visitor asked for a human'),
        ],
        string='AI Pause Reason',
        readonly=True,
        copy=False,
        help='Operator replied: the AI stays silent until the pause ends. Visitor '
             'asked for a human: the AI waits for an operator; if none replies, it '
             'answers the messages written meanwhile when the wait ends.',
    )
    ai_pause_notice_sent = fields.Boolean(
        string='AI Waiting Notice Sent',
        readonly=True,
        copy=False,
        help='The waiting notice was already sent during the current handoff wait.',
    )

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """
        Override _notify_thread to trigger AI responses for visitor messages.

        Called after each message is posted to the channel, inside the
        poster's transaction:
        - a human operator's message pauses the AI for the session (P1-2);
        - a visitor message asking for a human queues a handoff job (fixed
          notice + operator notification) and pauses the AI;
        - any other visitor message in an AI-enabled, non-paused session
          queues an im_livechat.ai.job in that same transaction (so the job
          exists exactly when the message is committed); processing starts
          after commit.
        """
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)

        try:
            # Savepoint: a failure here must not abort the poster's message.
            # sudo: the poster may be a guest, a portal or a public user.
            with self.env.cr.savepoint():
                self.sudo()._ai_handle_new_message(message.sudo())
        except Exception as e:
            _logger.warning(
                "Failed to trigger AI response for channel %s: %s",
                self.id, e, exc_info=True,
            )

        return rdata

    def _ai_handle_new_message(self, message):
        """Pause, hand off or queue an AI reply for a newly posted message."""
        if len(self) != 1 or not self._ai_session_enabled():
            return
        kind = self._ai_author_kind(message)
        if kind == AUTHOR_OPERATOR:
            if message.message_type == 'comment':
                # Operator pause; also ends a handoff wait (the operator took over).
                self._ai_pause(reason=PAUSE_OPERATOR)
            return
        # Only what the visitor writes is answered: not the rating/feedback
        # notification posted as the visitor after the chat, nor other
        # notifications.
        if message.message_type != 'comment':
            return
        if kind != AUTHOR_VISITOR:
            return
        channel = self.livechat_channel_id
        if self._ai_is_paused():
            if self.ai_pause_reason == PAUSE_HANDOFF:
                self._ai_handle_waiting_visitor(message)
            # Operator pause: no reply, no notice.
            return
        if channel._ai_is_handoff_request(_clean_text(message.body)):
            self._ai_pause(minutes=channel._ai_wait_minutes(), reason=PAUSE_HANDOFF)
            channel._trigger_ai_response(discuss_channel=self, message=message, kind='handoff')
        else:
            channel._trigger_ai_response(discuss_channel=self, message=message)

    def _ai_handle_waiting_visitor(self, message):
        """Visitor message during a handoff wait: the first one gets the
        waiting notice (once per wait), every one gets a deferred reply job
        due when the wait ends (the newest answers them all; an operator's
        reply cancels them)."""
        channel = self.livechat_channel_id
        if not self.ai_pause_notice_sent:
            # Written in the visitor's transaction: two concurrent visitor
            # posts serialize on this row, so only one notice is queued.
            self.write({'ai_pause_notice_sent': True})
            channel._trigger_ai_response(discuss_channel=self, message=message, kind='notice')
        channel._trigger_ai_response(discuss_channel=self, message=message, run_after=self.ai_paused_until)

    def _ai_session_enabled(self):
        self.ensure_one()
        # sudo: the ai_* fields are restricted to Live Chat managers.
        livechat_channel = self.sudo().livechat_channel_id
        return bool(
            self.channel_type == 'livechat'
            and livechat_channel
            and livechat_channel.ai_enabled
        )

    def _should_trigger_ai_response(self, message):
        """
        Determine if an AI response should be triggered for this message.

        Checks:
        1. Channel type is livechat
        2. Channel has associated livechat_channel_id
        3. AI integration is enabled for the channel
        4. Message is from a visitor (not operator, bot, or system)
        5. The AI is not paused for this session (human handoff)
        """
        if not self._ai_session_enabled():
            return False

        if not self._is_visitor_message(message):
            return False

        return not self._ai_is_paused()

    # --- Pause (human handoff, P1-2) ---

    def _ai_is_paused(self):
        """True while ai_paused_until is in the future (reads the committed
        value, so a running job sees an operator's post made meanwhile)."""
        self.ensure_one()
        self.invalidate_recordset(['ai_paused_until'])
        paused_until = self.sudo().ai_paused_until
        return bool(paused_until and paused_until > fields.Datetime.now())

    def _ai_pause(self, minutes=None, reason=PAUSE_OPERATOR):
        """Pause the AI for this session.

        reason 'operator': an operator replied (ai_handoff_pause_minutes by
        default). Between operator replies the pause is extended, never
        shortened. An operator reply during a handoff wait turns the wait
        into an operator pause that ends ai_handoff_pause_minutes after the
        reply, even when the wait would have lasted longer (a 120-minute
        wait becomes a 30-minute operator pause).
        reason 'handoff': the visitor asked for a human (called when the
        session is not paused); a new wait, its waiting notice not sent yet.
        """
        self.ensure_one()
        session = self.sudo()
        if minutes is None:
            minutes = session.livechat_channel_id._ai_pause_minutes()
        now = fields.Datetime.now()
        until = now + timedelta(minutes=minutes)
        current = session.ai_paused_until
        # The operator takes over an active handoff wait: its end no longer
        # applies, the operator pause starts now.
        taking_over = (
            reason == PAUSE_OPERATOR
            and session.ai_pause_reason == PAUSE_HANDOFF
            and current and current > now
        )
        vals = {}
        if taking_over or not current or current < until:
            vals['ai_paused_until'] = until
        if reason == PAUSE_HANDOFF:
            vals.update(ai_pause_reason=PAUSE_HANDOFF, ai_pause_notice_sent=False)
        elif session.ai_pause_reason != PAUSE_OPERATOR:
            vals['ai_pause_reason'] = PAUSE_OPERATOR
        if vals:
            session.write(vals)
        return session.ai_paused_until

    def _ai_waiting_for_human(self):
        """True during a handoff wait that no operator answered yet."""
        self.ensure_one()
        return self._ai_is_paused() and self.sudo().ai_pause_reason == PAUSE_HANDOFF

    # --- Who wrote a message (P1-1) ---

    def _ai_bot_ids(self):
        """Partner ids that are bots: the AI bots and OdooBot."""
        ids = set(self.env['im_livechat.channel']._ai_bot_partner_ids(self.sudo().livechat_channel_id))
        odoobot = self.env.ref('base.partner_root', raise_if_not_found=False)
        if odoobot:
            ids.add(odoobot.id)
        return ids

    def _ai_visitor_member(self):
        """The session's visitor member (partner or guest), or an empty recordset.

        Odoo creates the operator's member first and then, in the same
        transaction, the visitor's (a logged-in visitor's partner, or a guest
        member without partner; the LINE connector does the same); operators
        invited later get later members, created in a later transaction. So
        the visitor member is the first member created with the session that
        is neither the session operator nor a bot.
        """
        self.ensure_one()
        session = self.sudo()
        operator = session.livechat_operator_id
        bot_ids = self._ai_bot_ids()
        for member in session.channel_member_ids.sorted('id'):
            if member.create_date and session.create_date and member.create_date > session.create_date:
                break  # joined later: an invited operator, never the visitor
            partner = member.partner_id
            if partner and (partner == operator or partner.id in bot_ids):
                continue
            return member
        return self.env['discuss.channel.member']

    def _ai_visitor_partner(self):
        """Partner of the session's visitor member, or an empty recordset."""
        self.ensure_one()
        return self._ai_visitor_member().partner_id

    def _ai_author_kind(self, message, visitor_partner=None):
        """
        Classify the author of a message: visitor, operator or bot.

        Bot exclusion checks come FIRST to prevent response loops:
        - the channel's AI bot partner and the default AI bot partner
        - OdooBot (base.partner_root)
        Then:
        - guest (author_guest_id, e.g. LINE user) or no author: visitor
        - author with an internal (non-share) user: human operator, except
          when the author is the session's visitor member (an employee
          chatting on the website, e.g. the internal trial) and not the
          session operator: visitor
        - the session's assigned operator (livechat_operator_id) without an
          internal user, e.g. an Odoo chatbot: bot
        - anyone else, including portal users and the public user: visitor

        Args:
            visitor_partner (res.partner|None): result of
                _ai_visitor_partner(), when the caller classifies many messages
        """
        author = message.author_id
        if author and author.id in self._ai_bot_ids():
            return AUTHOR_BOT

        if message.author_guest_id or not author:
            return AUTHOR_VISITOR

        users = author.sudo().with_context(active_test=False).user_ids
        if any(not user.share for user in users):
            if author == self.sudo().livechat_operator_id:
                return AUTHOR_OPERATOR
            if visitor_partner is None:
                visitor_partner = self._ai_visitor_partner()
            if visitor_partner and author == visitor_partner:
                return AUTHOR_VISITOR
            return AUTHOR_OPERATOR
        if author == self.sudo().livechat_operator_id:
            return AUTHOR_BOT
        return AUTHOR_VISITOR

    def _is_visitor_message(self, message):
        """
        Check if a message is from a visitor (not operator, bot, or system).

        A visitor is a guest, an anonymous author, or a partner without any
        internal user account: portal users are visitors (P1-1). The AI bot
        partners, OdooBot, the session's assigned operator and internal users
        are never visitors.
        """
        return self._ai_author_kind(message) == AUTHOR_VISITOR

    def _ai_context_messages(self, livechat_channel):
        """The most recent ai_max_history comment/email messages of this
        session, oldest first (what the LLM gets as conversation)."""
        max_history = livechat_channel._ai_max_history_value()
        recent_messages = self.env['mail.message'].search(
            [
                ('res_id', '=', self.id),
                ('model', '=', 'discuss.channel'),
                ('message_type', 'in', ['comment', 'email']),
            ],
            order='id desc',
            limit=max_history,
        )
        return recent_messages.sorted('id')

    @staticmethod
    def _ai_operator_name(author):
        """Name visitors see for an operator: the Live Chat username when set."""
        author = author.sudo()
        user = author.with_context(active_test=False).user_ids.filtered(lambda u: not u.share)[:1]
        return (user.livechat_username if user else '') or author.name or ''

    # --- What the backend receives ---

    def _ai_installed_lang(self, *codes):
        """First of `codes` that is an installed language, else ''."""
        installed = {code for code, _name in self.env['res.lang'].get_installed()}
        return next((code for code in codes if code and code in installed), '')

    def _ai_session_lang(self):
        """Language of the website conversation (website_livechat), else the
        session operator's."""
        self.ensure_one()
        session = self.sudo()
        if 'livechat_visitor_id' in session._fields and session.livechat_visitor_id.lang_id:
            return session.livechat_visitor_id.lang_id.code
        return session.livechat_operator_id.lang or ''

    def _ai_visitor_kind(self):
        """guest, portal, employee, partner (no user) or public."""
        self.ensure_one()
        partner = self._ai_visitor_member().partner_id
        if not partner:
            return 'guest'
        users = partner.sudo().with_context(active_test=False).user_ids
        if not users:
            return 'partner'
        if all(user._is_public() for user in users):
            return 'public'
        if any(not user.share for user in users):
            return 'employee'
        return 'portal'

    def _ai_visitor_identity(self):
        """Who the AI talks to (woow_ai_contact docs/LIVECHAT.md visitor table).

        Never the IP address, visited pages, website.visitor tracking, phone
        or address. The email is only part of the n8n JSON, never of the
        text the model sees (openai metadata leaves it out).
        """
        self.ensure_one()
        session = self.sudo()
        member = self._ai_visitor_member()
        partner = member.partner_id
        guest = member.guest_id
        fallback_lang = self._ai_installed_lang(self._ai_session_lang(), session.livechat_operator_id.lang)
        identity = {
            'user_id': None, 'partner_id': None, 'guest_id': None, 'name': '', 'email': '',
            'lang': fallback_lang or '', 'tz': '', 'is_portal': False, 'is_guest': True,
            'company_id': None, 'visitor_key': '',
        }
        if partner:
            users = partner.with_context(active_test=False).user_ids
            if users and all(user._is_public() for user in users):
                # The public user's partner is shared by the whole site: no identity.
                identity['visitor_key'] = 'public'
                return identity
            user = users[:1]
            identity.update({
                'user_id': user.id or None,
                'partner_id': partner.id,
                'name': partner.name or '',
                'email': partner.email or '',
                'lang': self._ai_installed_lang((user.lang if user else partner.lang), fallback_lang),
                'tz': (user.tz if user else partner.tz) or '',
                'is_portal': bool(user and user._is_portal()),
                'is_guest': False,
                'company_id': user.company_id.id if user else None,
                'visitor_key': 'partner:%s' % partner.id,
            })
            return identity
        if guest:
            identity.update({
                'guest_id': guest.id,
                'name': guest.name or session.anonymous_name or '',
                'lang': self._ai_installed_lang(guest.lang, fallback_lang),
                'tz': guest.timezone or '',
                'visitor_key': 'guest:%s' % guest.id,
            })
        else:
            identity['name'] = session.anonymous_name or ''
        return identity

    def _ai_handoff_available(self):
        """True when the visitor can be handed to a human: the Live Chat
        channel has an operator who is an internal user (the openai system
        line and n8n ``channel.livechat.handoff_available``)."""
        self.ensure_one()
        livechat_channel = self.sudo().livechat_channel_id
        return bool(livechat_channel) and any(not user.share for user in livechat_channel.user_ids)

    def _ai_channel_info(self):
        """``channel`` of the n8n payload."""
        self.ensure_one()
        session = self.sudo()
        livechat_channel = session.livechat_channel_id
        operator = session.livechat_operator_id
        name = livechat_channel.name or ''
        return {
            'id': session.id,
            'type': 'livechat',
            'livechat': {
                # woow_ai_contact_livechat names, and the short aliases
                'livechat_channel_id': livechat_channel.id,
                'livechat_channel_name': name,
                'channel_id': livechat_channel.id,
                'channel_name': name,
                'operator_name': self._ai_operator_name(operator) if operator else '',
                'country_code': (session.country_id.code or '') if session.country_id else '',
                'visitor_kind': self._ai_visitor_kind(),
                'handoff_available': self._ai_handoff_available(),
            },
        }

    def _ai_conversation(self, livechat_channel, recent_messages=None, resume=False):
        """
        What the backend gets for one reply, independent of the protocol.

        History roles (woow_ai_contact protocol): visitor -> user; AI bot ->
        assistant; human operator -> user marked "[真人客服 name]" (the name
        visitors see), so the model treats it as a colleague's words and
        never as its own. Odoo adds no persona:
        only the channel's optional system prompt and conversation events
        (facts): operator replies in the history, and the end of a handoff
        wait that no operator answered.

        Args:
            livechat_channel (im_livechat.channel): The livechat channel config
            recent_messages (mail.message|None): result of
                _ai_context_messages() when the caller already fetched it
            resume (bool): the reply ends a handoff wait no operator answered

        Visitor messages keep their attachments (owner decision 39); the
        attachments of bot and operator messages are never used. A visitor
        message with attachments and no text is kept.

        Returns:
            dict: history (list of {role, text, message_id, kind,
                attachments}, oldest first; kind: visitor / operator / bot;
                attachments: ir.attachment (sudo), empty unless visitor),
                system_prompt (str), events (list of {time, kind, text})
        """
        if recent_messages is None:
            recent_messages = self._ai_context_messages(livechat_channel)
        visitor_partner = self._ai_visitor_partner()
        Attachment = self.env['ir.attachment'].sudo()
        # One prefetch set for the attachment metadata of the whole context
        # (the content is read one file at a time, see attachment_raw).
        prefetch = tuple(recent_messages.sudo().attachment_ids.ids)
        history = []
        first_operator_date = None
        for msg in recent_messages:
            clean_body = _clean_text(msg.body)
            kind = self._ai_author_kind(msg, visitor_partner=visitor_partner)
            attachments = Attachment
            if kind == AUTHOR_VISITOR:
                attachments = msg.sudo().attachment_ids.with_prefetch(prefetch).sorted('id')
            if not clean_body and not attachments:
                continue
            if kind == AUTHOR_VISITOR:
                role = 'user'
            elif kind == AUTHOR_OPERATOR:
                role = 'user'
                first_operator_date = first_operator_date or msg.date
                clean_body = (OPERATOR_PREFIX % self._ai_operator_name(msg.author_id)) + clean_body
            else:
                role = 'assistant'
            history.append({'role': role, 'text': clean_body, 'message_id': msg.id, 'kind': kind,
                            'attachments': attachments})
        events = []
        if first_operator_date:
            events.append({'time': first_operator_date, 'kind': 'operator', 'text': OPERATOR_SYSTEM_NOTE})
        if resume:
            events.append({'time': fields.Datetime.now(), 'kind': 'resume', 'text': RESUME_SYSTEM_NOTE})
        return {
            'history': history,
            'system_prompt': (livechat_channel.sudo().ai_system_prompt or '').strip(),
            'events': events,
        }

    # --- Visitor attachments (owner decision 39) ---

    @api.model
    def _ai_attachment_secret(self):
        return self.env['ir.config_parameter'].sudo().get_param('database.secret') or ''

    @api.model
    def _ai_attachment_base_url(self):
        """web.base.url: must be the public address of the site, the backends
        download the files from there."""
        return (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')

    @api.model
    def _ai_attachment_info(self, attachment, base_url=None, now=None):
        """{id, name, mimetype, size, url, expires_at} of a visitor attachment,
        with a signed download URL valid SIGNED_URL_TTL (1 hour)."""
        attachment = attachment.sudo()
        if base_url is None:
            base_url = self._ai_attachment_base_url()
        token, expires = make_token(self._ai_attachment_secret(), attachment.id, attachment.checksum, now=now)
        return {
            'id': attachment.id,
            'name': attachment.name or '',
            'mimetype': attachment.mimetype or 'application/octet-stream',
            'size': attachment.file_size or 0,
            'url': '%s%s/%s/%s' % (base_url, ATTACHMENT_ROUTE, attachment.id, token),
            'expires_at': iso_utc(expires),
        }

    @staticmethod
    def _ai_attachment_line(info, target=None):
        """``[附件] <name>（<mimetype>，<size>）：<signed url or target>``"""
        return ATTACHMENT_LINE % (info['name'] or 'file', info['mimetype'], human_size(info['size']),
                                  target or info['url'])

    @staticmethod
    def _ai_attachment_plan(history, document_mode):
        """What the openai request carries for each visitor attachment.

        Returns {(history position, attachment id): {'image': data URI} |
        {'file': OpenAI file part} | {'note': '[附件說明] ...'}}; any other
        attachment (older, link mode, dangerous or active type) gets no entry
        and is sent as its signed link only.

        - Images (type from the content): the INLINE_IMAGE_MESSAGES most
          recent visitor messages, newest first, at most
          INLINE_IMAGE_MAX_COUNT, INLINE_FILE_MAX_BYTES each; both modes.
        - Documents (DOCUMENT_TYPES: PDF, Word, Excel, text), 'file' mode: the
          INLINE_DOC_MESSAGES most recent visitor messages, newest first, at
          most INLINE_DOC_MAX_COUNT, original bytes and mimetype,
          INLINE_FILE_MAX_BYTES each.
        - Images first, then documents share INLINE_DATA_BUDGET data URI
          characters. A file skipped because of a limit gets a note.
        """
        visitor_positions = [i for i, entry in enumerate(history) if entry['kind'] == AUTHOR_VISITOR]
        image_window = set(visitor_positions[-INLINE_IMAGE_MESSAGES:])
        doc_window = set(visitor_positions[-INLINE_DOC_MESSAGES:]) if document_mode == DOCUMENT_MODE_FILE else set()
        plan = {}
        cache = {}
        budget = [INLINE_DATA_BUDGET]

        def content(attachment):
            """(raw bytes or None when too large, sniffed mimetype, link only)."""
            if attachment.id not in cache:
                if (attachment.file_size or 0) > INLINE_FILE_MAX_BYTES:
                    cache[attachment.id] = (None, None, is_link_only(attachment.name, attachment.mimetype))
                else:
                    raw = attachment_raw(attachment)
                    if len(raw) > INLINE_FILE_MAX_BYTES:
                        cache[attachment.id] = (None, None, is_link_only(attachment.name, attachment.mimetype))
                    else:
                        cache[attachment.id] = (raw, sniff_mimetype(raw),
                                                is_link_only(attachment.name, attachment.mimetype, raw))
            return cache[attachment.id]

        def note(name, reason):
            return {'note': ATTACHMENT_NOTE % (name, reason)}

        def too_large(attachment, kind):
            return note(attachment.name or 'file', '%s太大（%s，上限 %s），只附下載連結。' % (
                kind, human_size(attachment.file_size), human_size(INLINE_FILE_MAX_BYTES)))

        budget_reason = '這次請求的附件總量已達上限（%s），只附下載連結。' % human_size(INLINE_DATA_BUDGET)

        images = 0
        for position in sorted(image_window, reverse=True):
            for attachment in history[position]['attachments']:
                if is_link_only(attachment.name, attachment.mimetype):
                    continue
                key = (position, attachment.id)
                raw, sniffed, link_only = content(attachment)
                if raw is None:
                    if plain_mimetype(attachment.mimetype) in INLINE_IMAGE_TYPES:
                        plan[key] = too_large(attachment, '圖片')
                    continue
                if link_only or sniffed not in INLINE_IMAGE_TYPES:
                    continue
                if images >= INLINE_IMAGE_MAX_COUNT:
                    plan[key] = note(attachment.name or 'file',
                                     '每次最多附上 %s 張圖片，這張只附下載連結。' % INLINE_IMAGE_MAX_COUNT)
                    continue
                if data_uri_length(sniffed, len(raw)) > budget[0]:
                    plan[key] = note(attachment.name or 'file', budget_reason)
                    continue
                uri = make_data_uri(sniffed, raw)
                budget[0] -= len(uri)
                images += 1
                plan[key] = {'image': uri}

        documents = 0
        for position in sorted(doc_window, reverse=True):
            for attachment in history[position]['attachments']:
                key = (position, attachment.id)
                if key in plan or is_link_only(attachment.name, attachment.mimetype):
                    continue
                raw, sniffed, link_only = content(attachment)
                if raw is None:
                    extension = file_extension(attachment.name)
                    if extension in DOCUMENT_TYPES or plain_mimetype(attachment.mimetype) == 'application/pdf':
                        plan[key] = too_large(attachment, '檔案')
                    continue
                if link_only or sniffed in INLINE_IMAGE_TYPES or not raw:
                    continue  # dangerous, an image (older or already decided), or empty
                mimetype = document_mimetype(attachment.name, attachment.mimetype, raw)
                if not mimetype:
                    continue  # not a document the backend reads (archive, media...): a link
                if documents >= INLINE_DOC_MAX_COUNT:
                    plan[key] = note(attachment.name or 'file',
                                     '每次最多附上 %s 個檔案，這個只附下載連結。' % INLINE_DOC_MAX_COUNT)
                    continue
                if data_uri_length(mimetype, len(raw)) > budget[0]:
                    plan[key] = note(attachment.name or 'file', budget_reason)
                    continue
                uri = make_data_uri(mimetype, raw)
                budget[0] -= len(uri)
                documents += 1
                plan[key] = {'file': {'type': 'file', 'file': {'filename': attachment.name or 'file',
                                                               'file_data': uri}}}
        return plan

    def _ai_openai_messages(self, conversation, livechat_channel=None):
        """Chat completion messages: a system message only when there is a
        prompt or an event note, then the turns, which start and end with a
        user turn (see normalize_turns).

        A visitor turn with attachments (owner decision 39) carries them as
        they are: images as ``image_url`` data URIs, documents in 'file' mode
        as ``file`` parts with the original bytes and mimetype (plus a
        ``[附件]`` line), every other attachment as a ``[附件]`` line with its
        signed download URL (and a ``[附件說明]`` note when a limit skipped
        it). Such a turn's content becomes a list of parts.

        When the channel has operators, HANDOFF_SYSTEM_LINE ends the system
        message: the model ends its reply with [[handoff]] to hand over."""
        livechat_channel = livechat_channel or self.sudo().livechat_channel_id
        mode = (livechat_channel.sudo().ai_document_mode if livechat_channel else None) or DOCUMENT_MODE_FILE
        parts = [conversation['system_prompt']] + [event['text'] for event in conversation['events']]
        if self._ai_handoff_available():
            parts.append(HANDOFF_SYSTEM_LINE)  # always the last line (owner decision 40)
        system = '\n\n'.join(p.strip() for p in parts if p and p.strip())
        history = conversation['history']
        has_attachments = any(entry.get('attachments') for entry in history)
        plan = self._ai_attachment_plan(history, mode) if has_attachments else {}
        base_url = self._ai_attachment_base_url() if has_attachments else ''
        now = time.time()
        turns = []
        for position, entry in enumerate(history):
            attachments = entry.get('attachments')
            if not attachments:
                turns.append({'role': entry['role'], 'content': entry['text']})
                continue
            lines = [entry['text']] if entry['text'] else []
            images, files = [], []
            for attachment in attachments:
                decided = plan.get((position, attachment.id)) or {}
                if decided.get('image'):
                    images.append({'type': 'image_url', 'image_url': {'url': decided['image']}})
                    continue
                info = self._ai_attachment_info(attachment, base_url=base_url, now=now)
                if decided.get('file'):
                    files.append(decided['file'])
                    lines.append(self._ai_attachment_line(info, ATTACHED_TEXT))
                    continue
                lines.append(self._ai_attachment_line(info))
                if decided.get('note'):
                    lines.append(decided['note'])
            text = '\n'.join(lines)
            if images or files:
                content = ([{'type': 'text', 'text': text}] if text else []) + images + files
            else:
                content = text
            turns.append({'role': entry['role'], 'content': content})
        turns = normalize_turns(turns)
        return ([{'role': 'system', 'content': system}] if system else []) + turns

    def _build_llm_messages(self, livechat_channel, recent_messages=None, resume=False):
        """OpenAI chat completion messages of this session (see _ai_conversation)."""
        return self._ai_openai_messages(
            self._ai_conversation(livechat_channel, recent_messages=recent_messages, resume=resume),
            livechat_channel=livechat_channel)
