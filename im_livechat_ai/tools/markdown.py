# -*- coding: utf-8 -*-
"""A small, dependency-free markdown subset -> safe HTML renderer.

Used for every AI reply posted in a live chat (both backends); the same
renderer as woow_ai_contact (tools/markdown.py), adapted to this module's
link rule. Odoo 18 ships no markdown library, so no dependency is added.
Everything the backend sends is treated as untrusted text: each piece of
text is escaped first and only then wrapped in the few tags generated here;
the result is passed through Odoo's ``html_sanitize`` as a second line of
defence.

Supported (enough for chat replies):

- paragraphs, hard line breaks (a newline inside a paragraph is a ``<br/>``)
- ``**bold**`` / ``__bold__``, ``*italic*`` / ``_italic_`` (``<i>``; ``2*3*4`` and
  ``* item`` bullets stay as they are), ``~~strike~~``
- inline ``code`` and fenced code blocks (```lang ... ```)
- headings ``#`` .. ``######``, all rendered as ``h4`` (a chat bubble)
- unordered (``-``, ``*``, ``+``) and ordered (``1.``) lists, nested by indent
- block quotes (``>``) and horizontal rules (``---``), as styled ``div``
  (``blockquote``/``hr`` would be folded by Discuss as mail quotes)
- links ``[text](url)``, images ``![alt](url)`` (rendered as links: files are
  attached separately) and bare URLs; only http(s) links whose host is one of
  ``link_hosts`` (the store's own hosts) become links, every other link is
  plain text (its label, or the URL itself), against prompt-injected
  phishing links
- GFM tables (``| a | b |`` + ``|---|:-:|``), rendered as
  ``<table class="table table-sm table-bordered">``
"""

import re
from contextvars import ContextVar
from urllib.parse import urlsplit

from markupsafe import Markup, escape

from odoo.tools import html_sanitize

ALLOWED_SCHEMES = ('http://', 'https://')
MAX_INPUT_LENGTH = 100000  # characters; longer replies are cut (with a marker)

_FENCE_RE = re.compile(r'^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$')
_HEADING_RE = re.compile(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
_HR_RE = re.compile(r'^\s{0,3}([-*_])(\s*\1){2,}\s*$')
_LIST_RE = re.compile(r'^(?P<indent>\s*)(?P<marker>[-*+]|\d{1,9}[.)])\s+(?P<text>.*)$')
_QUOTE_RE = re.compile(r'^\s{0,3}>\s?(.*)$')
_TABLE_SEP_CELL_RE = re.compile(r'^\s*:?-{1,}:?\s*$')

# Inline tokens, in priority order. Code spans are handled before this.
_INLINE_RE = re.compile(
    r'(?P<image>!\[(?P<img_alt>[^\]\n]{0,300})\]\((?P<img_url>[^\s()<>]{1,2000})(?:\s+"[^"\n]*")?\))'
    r'|(?P<link>\[(?P<link_text>[^\]\n]{1,500})\]\((?P<link_url>[^\s()<>]{1,2000})(?:\s+"[^"\n]*")?\))'
    r'|(?P<autolink><(?P<auto_url>(?:https?://|mailto:)[^\s<>]{1,2000})>)'
    r"|(?P<bare>(?:https?://)[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]{1,2000})"
)
_URL_TRAILING_PUNCTUATION = '.,;:!?*\'"'
_BOLD_RE = re.compile(r'(\*\*|__)(?=\S)(.+?)(?<=\S)\1')
# Not next to an ASCII letter or digit (2*3*4, a*b*c stay as they are), but
# next to CJK text (這是*斜體*的) it is italics.
_ITALIC_STAR_RE = re.compile(r'(?<![A-Za-z0-9_*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![A-Za-z0-9_*])')
_ITALIC_UNDER_RE = re.compile(r'(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])')
_STRIKE_RE = re.compile(r'~~(?=\S)(.+?)(?<=\S)~~')

_LINK_MARKUP = Markup('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>')

# Allowed link hosts of the reply being rendered (empty: no links at all)
_LINK_HOSTS = ContextVar('im_livechat_ai_link_hosts', default=frozenset())


def url_host(url):
    try:
        return (urlsplit(url).hostname or '').lower().rstrip('.')
    except ValueError:
        return ''


def safe_url(url):
    """Return the URL when its scheme is allowed (http, https), else None."""
    url = (url or '').strip()
    if url.lower().startswith(ALLOWED_SCHEMES) and not any(c in url for c in '\r\n\t'):
        return url
    return None


def _emphasis(escaped):
    """Bold / italic / strike on already-escaped text (a Markup string)."""
    text = str(escaped)
    text = _BOLD_RE.sub(r'<strong>\2</strong>', text)
    text = _STRIKE_RE.sub(r'<del>\1</del>', text)
    text = _ITALIC_STAR_RE.sub(r'<i>\1</i>', text)
    text = _ITALIC_UNDER_RE.sub(r'<i>\1</i>', text)
    return Markup(text)


def _link_allowed(url):
    return url_host(url) in _LINK_HOSTS.get()


def _link(url, label):
    url = safe_url(url)
    if not url or not _link_allowed(url):
        return _emphasis(escape(label))
    return _LINK_MARKUP % (url, _emphasis(escape(label)))


def _inline_no_code(text):
    parts = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        parts.append(_emphasis(escape(text[position:match.start()])))
        if match.group('image'):
            label = match.group('img_alt') or match.group('img_url')
            parts.append(_link(match.group('img_url'), label))
        elif match.group('link'):
            parts.append(_link(match.group('link_url'), match.group('link_text')))
        elif match.group('autolink'):
            parts.append(_link(match.group('auto_url'), match.group('auto_url')))
        else:
            url = match.group('bare')
            stripped = url.rstrip(_URL_TRAILING_PUNCTUATION)
            # an unbalanced closing parenthesis is punctuation, not URL
            while stripped.endswith(')') and stripped.count('(') < stripped.count(')'):
                stripped = stripped[:-1]
            tail = url[len(stripped):]
            parts.append(_link(stripped, stripped))
            parts.append(escape(tail))
        position = match.end()
    parts.append(_emphasis(escape(text[position:])))
    return Markup('').join(parts)


def render_inline(text):
    """One line (or table cell) of markdown -> Markup."""
    parts = []
    # code spans first: their content is never formatted
    pieces = re.split(r'(`+)(.+?)\1', text)
    # re.split with 2 groups yields [text, ticks, code, text, ticks, code, ...]
    i = 0
    while i < len(pieces):
        parts.append(_inline_no_code(pieces[i]))
        if i + 2 < len(pieces):
            parts.append(Markup('<code>%s</code>') % pieces[i + 2].strip())
        i += 3
    return Markup('').join(parts)


def _split_row(line):
    line = line.strip()
    if line.startswith('|'):
        line = line[1:]
    if line.endswith('|') and not line.endswith('\\|'):
        line = line[:-1]
    cells, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '|':
            cells.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append(''.join(current).strip())
    return cells


def _is_table_start(lines, i):
    if i + 1 >= len(lines) or '|' not in lines[i]:
        return False
    sep = lines[i + 1]
    if '|' not in sep and '-' not in sep:
        return False
    cells = _split_row(sep)
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells) and len(_split_row(lines[i])) >= 1


def _render_table(lines, i):
    header = _split_row(lines[i])
    aligns = []
    for cell in _split_row(lines[i + 1]):
        cell = cell.strip()
        if cell.startswith(':') and cell.endswith(':'):
            aligns.append('center')
        elif cell.endswith(':'):
            aligns.append('right')
        elif cell.startswith(':'):
            aligns.append('left')
        else:
            aligns.append(None)
    ncols = len(header)
    aligns = (aligns + [None] * ncols)[:ncols]

    def cell_html(tag, value, align):
        if align:
            return Markup('<%s style="text-align: %s">%s</%s>') % (
                Markup(tag), Markup(align), render_inline(value), Markup(tag))
        return Markup('<%s>%s</%s>') % (Markup(tag), render_inline(value), Markup(tag))

    out = [Markup('<table class="table table-sm table-bordered"><thead><tr>')]
    out += [cell_html('th', h, a) for h, a in zip(header, aligns)]
    out.append(Markup('</tr></thead><tbody>'))
    i += 2
    while i < len(lines) and lines[i].strip() and '|' in lines[i]:
        row = (_split_row(lines[i]) + [''] * ncols)[:ncols]
        out.append(Markup('<tr>'))
        out += [cell_html('td', c, a) for c, a in zip(row, aligns)]
        out.append(Markup('</tr>'))
        i += 1
    out.append(Markup('</tbody></table>'))
    return Markup('').join(out), i


def _render_list(lines, i):
    """Parse a (possibly nested) list starting at lines[i]; returns (Markup, next index)."""
    first = _LIST_RE.match(lines[i])
    base_indent = len(first.group('indent').expandtabs(4))
    ordered = first.group('marker')[0].isdigit()
    tag = 'ol' if ordered else 'ul'
    items = []  # list of [text_lines, children_markup]
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            # a blank line ends the list unless the list continues right after
            if i + 1 < len(lines) and _LIST_RE.match(lines[i + 1]) and \
                    len(_LIST_RE.match(lines[i + 1]).group('indent').expandtabs(4)) >= base_indent:
                i += 1
                continue
            break
        match = _LIST_RE.match(line)
        if match:
            indent = len(match.group('indent').expandtabs(4))
            if indent < base_indent:
                break
            if indent >= base_indent + 2 and items:
                child, i = _render_list(lines, i)
                items[-1][1].append(child)
                continue
            if match.group('marker')[0].isdigit() != ordered:
                break
            items.append([[match.group('text')], []])
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if items and indent >= base_indent + 2:
            items[-1][0].append(line.strip())  # continuation line of the item
            i += 1
            continue
        break
    out = [Markup('<%s>') % Markup(tag)]
    for text_lines, children in items:
        body = Markup('<br/>').join(render_inline(t) for t in text_lines)
        out.append(Markup('<li>%s%s</li>') % (body, Markup('').join(children)))
    out.append(Markup('</%s>') % Markup(tag))
    return Markup('').join(out), i


def _is_block_start(lines, i):
    line = lines[i]
    return bool(
        _FENCE_RE.match(line) or _HEADING_RE.match(line) or _HR_RE.match(line)
        or _LIST_RE.match(line) or _QUOTE_RE.match(line) or _is_table_start(lines, i)
    )


def _render_blocks(lines):
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3] if marker[0] == '`' else marker):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of text)
            out.append(Markup('<pre><code>%s</code></pre>') % '\n'.join(code))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            out.append(Markup('<h4>%s</h4>') % render_inline(heading.group(2)))
            i += 1
            continue
        if _HR_RE.match(line):
            # not <hr>: Odoo tags it as a mail quote and Discuss hides it
            out.append(Markup('<div class="border-top my-2"></div>'))
            i += 1
            continue
        if _is_table_start(lines, i):
            table, i = _render_table(lines, i)
            out.append(table)
            continue
        if _LIST_RE.match(line):
            block, i = _render_list(lines, i)
            out.append(block)
            continue
        if _QUOTE_RE.match(line):
            quoted = []
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                quoted.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            # not <blockquote>: Odoo tags it as a mail quote and Discuss folds it
            out.append(Markup('<div class="border-start border-3 ps-2 text-muted">%s</div>') % _render_blocks(quoted))
            continue
        paragraph = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines, i):
            paragraph.append(lines[i].strip())
            i += 1
        out.append(Markup('<p>%s</p>') % Markup('<br/>').join(render_inline(p) for p in paragraph))
    return Markup('').join(out)


def markdown_to_html(text, link_hosts=()):
    """Markdown subset -> sanitized HTML (Markup). Empty input -> empty Markup.

    Args:
        text (str): markdown
        link_hosts (iterable): hosts (exact, lower case) whose http(s) links
            become ``<a>``; other links are rendered as their text
    """
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH] + '\n\n…'
    if not text.strip():
        return Markup('')
    token = _LINK_HOSTS.set(frozenset(host.lower().rstrip('.') for host in link_hosts or () if host))
    try:
        html = _render_blocks(text.split('\n'))
    finally:
        _LINK_HOSTS.reset(token)
    return Markup(html_sanitize(html, sanitize_attributes=True, sanitize_style=True))
