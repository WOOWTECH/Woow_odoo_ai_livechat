# -*- coding: utf-8 -*-
"""Backend helpers shared by the AI connection, the AI assistant and the Live
Chat channel (no database access, no Odoo model).

They lived in models/im_livechat_channel.py up to 18.0.1.3.5; they moved here
with 18.0.2.0.0 so the connection model can use them without an import cycle.
"""

import hashlib
import hmac
import json
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytz
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

BACKEND_OPENAI = 'openai'  # technical value of LiteLLM (the OpenAI chat-completions protocol)
BACKEND_N8N = 'n8n'

# The current time of a request (contract 3.6, 3.7): English day names, and the one line of the LiteLLM system
# message (literal protocol text, never translated).
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
TIME_LINE = 'Current time: %s %s (%s), time zone %s (UTC%s).'

# Plain http:// is accepted only for these hosts (aligned with Discuss AI).
HTTP_ALLOWED_HOSTS = ('localhost', '127.0.0.1', '::1')
HTTP_ALLOWED_SUFFIX = '.svc.cluster.local'


def endpoint_url_allowed(url):
    """https://, or http:// to localhost, 127.0.0.1, ::1 or an in-cluster
    host ending in .svc.cluster.local (a short service name is refused)."""
    try:
        parts = urlsplit((url or '').strip())
        host = (parts.hostname or '').lower()
    except ValueError:
        return False
    if not host:
        return False
    scheme = (parts.scheme or '').lower()
    if scheme == 'https':
        return True
    if scheme == 'http':
        return host in HTTP_ALLOWED_HOSTS or host.endswith(HTTP_ALLOWED_SUFFIX)
    return False


def endpoint_origin(url):
    """(scheme, host, port) of an endpoint URL, None when empty: the server a
    key is sent to."""
    url = (url or '').strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ('?', url)
    scheme = (parts.scheme or '').lower()
    return (scheme, (parts.hostname or '').lower(), port or {'https': 443, 'http': 80}.get(scheme))


def normalize_openai_url(url):
    """LiteLLM base URL as the runtime uses it: trimmed, no trailing slash,
    no /chat/completions."""
    url = (url or '').strip().rstrip('/')
    if url.endswith('/chat/completions'):
        url = url[:-len('/chat/completions')].rstrip('/')
    return url


def json_dumps(payload):
    """Compact UTF-8 JSON: the exact bytes that are signed and sent to n8n."""
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


def n8n_signature_headers(secret, body, timestamp=None):
    """n8n request signature: ``sha256=`` + hex HMAC-SHA256(secret,
    "<timestamp>.<body bytes>"). The secret itself is never sent."""
    timestamp = str(int(timestamp if timestamp is not None else time.time()))
    digest = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    return {'X-Woow-Timestamp': timestamp, 'X-Woow-Signature': 'sha256=%s' % digest}


def request_not_sent(error):
    """True when a requests.ConnectionError happened before the request was
    sent: a connect timeout or a connection that could not be opened
    (refused, unknown host). A connection reset or closed after the request
    was sent is not: the backend may already be working on it."""
    if isinstance(error, requests.ConnectTimeout):
        return True
    if isinstance(error, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if not isinstance(error, requests.ConnectionError):
        return False
    reason = error.args[0] if getattr(error, 'args', None) else None
    if isinstance(reason, MaxRetryError):
        reason = reason.reason
    return isinstance(reason, NewConnectionError)


def person_name(name):
    """A person's name as sent to a backend (contract 3.6, 3.14): a name with
    an '@' (a partner named after an email address) counts as no name."""
    name = name or ''
    return '' if '@' in name else name


def one_line_name(name):
    """A person's name inside text the model reads (contract 3.7, 3.14): one
    line (white space collapsed), brackets turned into parentheses, at most 80
    characters; a name with an '@' counts as no name."""
    name = ' '.join((name or '').replace('[', '(').replace(']', ')').split())
    return '' if '@' in name else name[:80]


def utc_now():
    """The module's clock: the current instant, an aware datetime in UTC. The current time of a request is
    read from here only, so a test can freeze it."""
    return datetime.now(timezone.utc)


def known_time_zone(name):
    """The IANA name of the time zone ``name`` (pytz, as Odoo validates time zones), or '' when ``name`` is
    empty or not a known IANA time zone (it counts as none, contract 3.7)."""
    if not isinstance(name, str) or not name:
        return ''
    try:
        return pytz.timezone(name).zone
    except (pytz.UnknownTimeZoneError, ValueError):
        return ''


def utc_offset(moment):
    """'+08:00' / '-05:30' / '+00:00': the UTC offset of an aware datetime."""
    seconds = int(moment.utcoffset().total_seconds())
    hours, minutes = divmod(abs(seconds) // 60, 60)
    return '%s%02d:%02d' % ('-' if seconds < 0 else '+', hours, minutes)


def current_time(zone, now=None):
    """The current time of one request (contract 3.6, 3.7): ``{utc, local, tz, weekday}``.

    - ``utc``: ``YYYY-MM-DDTHH:MM:SSZ``;
    - ``local``: the same instant in ``zone``, ``YYYY-MM-DDTHH:MM:SS±HH:MM``;
    - ``tz``: the IANA name of the zone (``UTC`` when ``zone`` is not a known time zone);
    - ``weekday``: the English name of the day of ``local``.

    ``now``: an aware datetime (default: utc_now())."""
    tz = pytz.timezone(known_time_zone(zone) or 'UTC')
    now = (now or utc_now()).astimezone(timezone.utc)
    local = now.astimezone(tz)
    return {
        'utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'local': local.strftime('%Y-%m-%dT%H:%M:%S') + utc_offset(local),
        'tz': tz.zone,
        'weekday': WEEKDAYS[local.weekday()],
    }


def time_line(now):
    """The LiteLLM system line of a current_time() value (contract 3.7):
    ``Current time: YYYY-MM-DD HH:MM (<weekday>), time zone <tz> (UTC±HH:MM).``"""
    local = now['local']
    return TIME_LINE % (local[:10], local[11:16], now['weekday'], now['tz'], local[19:])


def exception_frames(error):
    """What the server log says about an unexpected exception: its type name
    and its code frames, never its text (the text of an HTTP library error can
    hold a header value, an upstream body or a key; contract 3.9)."""
    frames = ''.join(traceback.format_tb(getattr(error, '__traceback__', None))).rstrip()
    name = type(error).__name__
    return '%s\n%s' % (name, frames) if frames else name
