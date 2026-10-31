# -*- coding: utf-8 -*-
"""Read-only product lookup tools for the live chat AI (package E, P1-8).

The LLM gets two OpenAI-style function tools through LiteLLM:

  search_products(keyword, category?, max_price?)
      up to 8 published product templates: id, name, website price range,
      short description, categories and the absolute website URL
  get_product_details(product_id | name, options?)
      one published template: description, attributes, the variants
      (attribute values, website price, URL) and the optional (no-variant)
      attributes with their extra price

Everything is read with the ORM inside Odoo (no MCP, no HTTP). Only products
that are published on the channel's website and can be sold are ever
returned. Prices are the website prices of the public (not logged-in)
visitor: website_sale's _get_combination_info, i.e. the variant's lst_price
through the website pricelist and tax display setting. Names are read in
zh_TW when that language is installed. URLs are web.base.url + website_url.

The tools need website_sale; without it the AI gets no tools and the system
prompt tells it to ask for the visitor's contact details instead of quoting.
"""

import copy
import json
import logging
import re

from odoo import api, models
from odoo.osv import expression
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

TOOL_SEARCH_PRODUCTS = 'search_products'
TOOL_PRODUCT_DETAILS = 'get_product_details'

SEARCH_RESULT_LIMIT = 8  # templates returned by search_products
SEARCH_CANDIDATE_LIMIT = 200  # rows read per ORM search before ranking
PRICE_SCAN_LIMIT = 40  # ranked templates priced while filling the result
PRICE_VARIANT_LIMIT = 100  # variants priced per template for a price range
DETAIL_VARIANT_LIMIT = 20  # variants listed by get_product_details
OTHER_MATCH_LIMIT = 4  # alternative matches listed when resolving by name
MAX_SEARCH_TERMS = 24
SHORT_DESCRIPTION_LENGTH = 160
DESCRIPTION_LENGTH = 1500
PREFERRED_LANG = 'zh_TW'

_TOKEN_SPLIT_RE = re.compile(r'[\s,，、;；/|+＋]+')
_OPTION_SPLIT_RE = re.compile(r'[,，、;；\n]+')
_CJK_RE = re.compile('[\u3400-\u9fff\uf900-\ufaff]')

NOT_FOUND_MESSAGE = (
    '官網上沒有找到符合的已上架商品。不要自行提供價格或規格，'
    '請訪客留下聯絡方式（姓名、電話或 Email），由客服人員協助報價。'
)

TOOL_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': TOOL_SEARCH_PRODUCTS,
            'description': (
                '搜尋官網上架中的商品。回傳最多 8 筆：商品編號 product_id、品名、'
                '官網售價區間（price_min～price_max，依規格不同）、幣別、簡短說明、'
                '分類與官網連結。訪客問到商品、價格或推薦時先用這個工具。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'keyword': {
                        'type': 'string',
                        'description': '商品關鍵字，例如「崁燈」「窗簾 馬達」「ZigBee 開關」；多個詞用空白分開。',
                    },
                    'category': {
                        'type': 'string',
                        'description': '選填。官網商品分類關鍵字，例如「照明」「開關」「窗簾」「感測」。',
                    },
                    'max_price': {
                        'type': 'number',
                        'description': '選填。最高價格（官網售價），只回傳起價不超過此金額的商品。',
                    },
                },
                'required': ['keyword'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': TOOL_PRODUCT_DETAILS,
            'description': (
                '取得一個官網上架商品的詳細資料：說明、規格選項、各規格組合的官網售價'
                '與連結、加購選項的加價。用 search_products 回傳的 product_id 查詢最準確，'
                '也可以用品名。訪客選定規格後，用 options 帶入規格值查該組合的實際價格。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'product_id': {
                        'type': 'integer',
                        'description': 'search_products 回傳的 product_id。',
                    },
                    'name': {
                        'type': 'string',
                        'description': '沒有 product_id 時用品名查詢。',
                    },
                    'options': {
                        'type': 'string',
                        'description': (
                            '選填。要篩選的規格值，用逗號分開，'
                            '例如「雙色溫, ZIGBEE電源, 2700K-6000K, 標準 Φ75mm」。'
                        ),
                    },
                },
            },
        },
    },
]


def _norm(text):
    return (text or '').strip().lower()


def _tokens(text):
    """Search words of a keyword string (lowercase, de-duplicated)."""
    return list(dict.fromkeys(t for t in _TOKEN_SPLIT_RE.split(_norm(text)) if t))


def _bigrams(token):
    return [token[i:i + 2] for i in range(len(token) - 1)]


def _search_terms(tokens):
    """Terms for the ORM prefilter: every token, plus the two-character
    pieces of Chinese tokens (visitors rarely type a product name exactly)."""
    terms = []
    for token in tokens:
        terms.append(token)
        if len(token) > 2 and _CJK_RE.search(token):
            terms.extend(_bigrams(token))
    return list(dict.fromkeys(terms))[:MAX_SEARCH_TERMS]


def _token_score(token, name_text, other_text):
    """Relevance of one search token for a product (0 = no match)."""
    if token in name_text:
        return 10.0
    if token in other_text:
        return 5.0
    if len(token) > 2 and _CJK_RE.search(token):
        grams = _bigrams(token)
        in_name = sum(1 for g in grams if g in name_text) / len(grams)
        in_other = sum(1 for g in grams if g in other_text) / len(grams)
        return max(6.0 * in_name, 3.0 * in_other)
    return 0.0


def _number(value):
    """JSON-friendly price: 1520 instead of 1520.0."""
    if value is None:
        return None
    value = round(value, 2)
    return int(value) if value == int(value) else value


def _truncate(text, length):
    text = re.sub(r'\n{3,}', '\n\n', (text or '').strip())
    return text if len(text) <= length else text[:length].rstrip() + '…'


class _ProductCatalog:
    """Published products of one website, read as the public visitor sees them."""

    def __init__(self, env, website, lang):
        # Superuser rights (no ACL / record rule surprises in the job thread)
        # but the website's public user, so prices use the public pricelist.
        public_user = website.user_id
        self.env = env(
            user=public_user.id if public_user else env.uid,
            su=True,
            context=dict(env.context, lang=lang, website_id=website.id),
        )
        self.website = self.env['website'].browse(website.id)
        self.lang = lang
        self.Template = self.env['product.template']
        base_url = self.env['ir.config_parameter'].get_param('web.base.url') or ''
        self.base_url = base_url.strip().rstrip('/')
        self.currency = self.website.currency_id
        self._price_cache = {}

    # --- Domain / helpers ---

    def _base_domain(self):
        """Only sellable products published on this website (P1-8)."""
        company = self.website.company_id
        return [
            ('sale_ok', '=', True),
            ('is_published', '=', True),
            ('company_id', 'in', [False, company.id]),
        ] + self.website.website_domain()

    def _langs(self):
        langs = [self.lang]
        if 'en_US' not in langs:
            langs.append('en_US')
        return langs

    def _url(self, path):
        if not path:
            return None
        if path.startswith(('http://', 'https://')):
            return path
        return self.base_url + path

    def _currency_name(self):
        return self.currency.name if self.currency else None

    def _variants(self, template):
        return template.product_variant_ids.sorted('id')

    def _variant_price(self, variant, extra_ptavs=None):
        """The price the website shows for this variant (public visitor)."""
        key = (variant.id, tuple(extra_ptavs.ids) if extra_ptavs else ())
        if key not in self._price_cache:
            template = variant.product_tmpl_id
            combination = variant.product_template_attribute_value_ids
            if extra_ptavs:
                combination |= extra_ptavs
            info = template._get_combination_info(combination=combination, product_id=variant.id)
            self._price_cache[key] = info['price']
        return self._price_cache[key]

    def _price_range(self, template):
        variants = self._variants(template)[:PRICE_VARIANT_LIMIT]
        prices = [self._variant_price(v) for v in variants]
        if not prices:
            return None, None
        return min(prices), max(prices)

    def _categories(self, template):
        return template.public_categ_ids.mapped('name')

    def _description(self, template, length):
        parts = [template.description_sale or '']
        if 'description_ecommerce' in template._fields and template.description_ecommerce:
            parts.append(html2plaintext(template.description_ecommerce))
        return _truncate('\n'.join(p for p in parts if p.strip()), length)

    # --- Matching ---

    def _find_categories(self, text):
        Category = self.env['product.public.category']
        ids = set()
        for lang in self._langs():
            ids.update(Category.with_context(lang=lang).search([('name', 'ilike', text.strip())]).ids)
        return list(ids)

    def _prefilter(self, domain, terms):
        """Templates matching any term in name, code, description, category
        or attribute value, in the visitor language and in English."""
        fields = [
            'name',
            'default_code',
            'product_variant_ids.default_code',
            'description_sale',
            'public_categ_ids.name',
            'attribute_line_ids.value_ids.name',
        ]
        term_domain = expression.OR([[(field, 'ilike', term)] for term in terms for field in fields])
        ids = set()
        for lang in self._langs():
            ids.update(self.Template.with_context(lang=lang).search(
                domain + term_domain, limit=SEARCH_CANDIDATE_LIMIT, order='website_sequence, id',
            ).ids)
        return self.Template.browse(sorted(ids))

    def _search_texts(self, templates):
        """{template id: (name text, other text)} in all languages."""
        texts = {t.id: ['', ''] for t in templates}
        for lang in self._langs():
            for template in templates.with_context(lang=lang):
                name_text, other_text = texts[template.id]
                values = template.attribute_line_ids.value_ids.mapped('name')
                codes = [c for c in [template.default_code] + template.product_variant_ids.mapped('default_code') if c]
                texts[template.id] = [
                    name_text + ' ' + _norm(template.name),
                    ' '.join([other_text, _norm(template.description_sale)]
                             + [_norm(x) for x in template.public_categ_ids.mapped('name') + values + codes]),
                ]
        return texts

    def _rank(self, templates, tokens):
        """Templates sorted by relevance; those matching no token are dropped."""
        if not tokens:
            return list(templates.sorted(lambda t: (t.website_sequence, t.id)))
        texts = self._search_texts(templates)
        scored = []
        for template in templates:
            name_text, other_text = texts[template.id]
            scores = [_token_score(tok, name_text, other_text) for tok in tokens]
            score = sum(scores)
            if score <= 0:
                continue
            if all(scores):
                score += 5.0  # every word matched
            scored.append((-score, template.website_sequence, template.id, template))
        scored.sort(key=lambda row: row[:3])
        return [row[3] for row in scored]

    def _search_ranked(self, keyword, category=None):
        """Return (ranked templates, notes)."""
        tokens = _tokens(keyword)
        domain = self._base_domain()
        notes = []
        if category and category.strip():
            category_ids = self._find_categories(category)
            if category_ids:
                domain += [('public_categ_ids', 'child_of', category_ids)]
            else:
                notes.append('官網沒有「%s」這個分類，已改用關鍵字搜尋。' % category.strip())
                tokens = list(dict.fromkeys(tokens + _tokens(category)))
        if tokens:
            candidates = self._prefilter(domain, _search_terms(tokens))
        else:
            candidates = self.Template.search(domain, limit=SEARCH_CANDIDATE_LIMIT, order='website_sequence, id')
        candidates = candidates.filtered('product_variant_ids')
        return self._rank(candidates, tokens), notes

    # --- Tools ---

    def search_products(self, keyword='', category=None, max_price=None):
        if not (keyword or '').strip() and not (category or '').strip():
            return {'error': '請提供商品關鍵字或分類。'}
        if max_price is not None:
            try:
                max_price = float(max_price)
            except (TypeError, ValueError):
                max_price = None
            if max_price is not None and max_price <= 0:
                max_price = None
        ranked, notes = self._search_ranked(keyword, category)
        results = []
        excluded_by_price = 0
        for template in ranked[:PRICE_SCAN_LIMIT]:
            price_min, price_max = self._price_range(template)
            if price_min is None:
                continue
            if max_price is not None and price_min > max_price:
                excluded_by_price += 1
                continue
            results.append({
                'product_id': template.id,
                'name': template.name,
                'price_min': _number(price_min),
                'price_max': _number(price_max),
                'currency': self._currency_name(),
                'variant_count': len(template.product_variant_ids),
                'short_description': self._description(template, SHORT_DESCRIPTION_LENGTH),
                'categories': self._categories(template),
                'url': self._url(template.website_url),
            })
            if len(results) >= SEARCH_RESULT_LIMIT:
                break
        if excluded_by_price:
            notes.append('另有 %s 個商品起價超過 %s，未列出。' % (excluded_by_price, _number(max_price)))
        response = {'results': results, 'count': len(results)}
        if not results:
            response['message'] = NOT_FOUND_MESSAGE
        else:
            response['note'] = (
                '價格為官網售價，price_min～price_max 是不同規格的價格區間；'
                '要報特定規格的價格請用 get_product_details 查詢。只能使用這裡的連結。'
            )
        if notes:
            response['notes'] = notes
        return response

    def _resolve(self, product_id=None, name=None):
        """Return (template or None, other matches)."""
        if product_id not in (None, '', False):
            try:
                product_id = int(product_id)
            except (TypeError, ValueError):
                product_id = None
            if product_id:
                template = self.Template.search(self._base_domain() + [('id', '=', product_id)], limit=1)
                template = template.filtered('product_variant_ids')
                if template or not name:
                    return template or None, []
        if name and name.strip():
            ranked, _notes = self._search_ranked(name)
            if ranked:
                return ranked[0], ranked[1:1 + OTHER_MATCH_LIMIT]
        return None, []

    def _option_filter(self, variants, options):
        """Variants whose attribute values match every recognised option.

        Returns (variants, matched option terms, unknown option terms).
        """
        terms = [t for t in (_norm(x) for x in _OPTION_SPLIT_RE.split(options or '')) if t]
        if not terms:
            return variants, [], []
        ptavs = variants.product_template_attribute_value_ids
        matched, unknown = [], []
        for term in terms:
            exact = ptavs.filtered(lambda v: _norm(v.name) == term)
            hits = exact or ptavs.filtered(lambda v: term in _norm(v.name)) \
                or ptavs.filtered(lambda v: _norm(v.name) and _norm(v.name) in term)
            if not hits:
                unknown.append(term)
                continue
            matched.append(term)
            variants = variants.filtered(lambda p: p.product_template_attribute_value_ids & hits)
        return variants, matched, unknown

    def get_product_details(self, product_id=None, name=None, options=None):
        if not product_id and not (name or '').strip():
            return {'error': '請提供 product_id 或品名。'}
        template, others = self._resolve(product_id=product_id, name=name)
        if not template:
            return {'error': '找不到這個商品，或商品沒有在官網上架。', 'message': NOT_FOUND_MESSAGE}

        all_variants = self._variants(template)
        variants, matched, unknown = self._option_filter(all_variants, options)
        variants = variants.sorted(lambda v: (self._variant_price(v), v.id))
        price_min, price_max = self._price_range(template)

        attributes, optional_attributes = [], []
        cheapest = all_variants.sorted(lambda v: (self._variant_price(v), v.id))[:1]
        for line in template.attribute_line_ids:
            values = line.product_template_value_ids.filtered('ptav_active')
            if line.attribute_id.create_variant == 'no_variant':
                base = self._variant_price(cheapest) if cheapest else 0.0
                optional_attributes.append({
                    'name': line.attribute_id.name,
                    'values': [{
                        'name': value.name,
                        'extra_price': _number(self._variant_price(cheapest, value) - base) if cheapest else None,
                    } for value in values],
                })
            else:
                attributes.append({'name': line.attribute_id.name, 'values': values.mapped('name')})

        listed = variants[:DETAIL_VARIANT_LIMIT]
        result = {
            'product_id': template.id,
            'name': template.name,
            'url': self._url(template.website_url),
            'currency': self._currency_name(),
            'price_min': _number(price_min),
            'price_max': _number(price_max),
            'categories': self._categories(template),
            'description': self._description(template, DESCRIPTION_LENGTH),
            'attributes': attributes,
            'variant_count': len(all_variants),
            'matched_variant_count': len(variants),
            'variants': [{
                'variant_id': variant.id,
                'options': {
                    ptav.attribute_id.name: ptav.name
                    for ptav in variant.product_template_attribute_value_ids
                },
                'price': _number(self._variant_price(variant)),
                'url': self._url(variant.website_url),
            } for variant in listed],
        }
        if optional_attributes:
            result['optional_attributes'] = optional_attributes
        notes = ['價格為官網售價，只能引用這裡的數字與連結。']
        if optional_attributes:
            notes.append('optional_attributes 是加購選項，選了要再加上 extra_price。')
        if len(variants) > len(listed):
            notes.append(
                '共有 %s 種符合的規格組合，只列出最便宜的 %s 種；請用 options 帶入訪客選的規格值再查一次。'
                % (len(variants), len(listed)))
        if matched and not variants:
            notes.append('沒有同時符合這些規格的組合，請向訪客確認規格。')
        if unknown:
            notes.append('這個商品沒有這些規格值：%s。' % '、'.join(unknown))
        if others:
            result['other_matches'] = [{'product_id': t.id, 'name': t.name} for t in others]
            notes.append('依品名找到最接近的商品；如果不是訪客要的，請看 other_matches。')
        result['notes'] = notes
        return result


class LivechatAiTools(models.AbstractModel):
    """Entry point for the product tools offered to the live chat LLM."""
    _name = 'im_livechat.ai.tools'
    _description = 'Live Chat AI Product Tools'

    @api.model
    def _tools_supported(self):
        """True when website_sale (published products, website prices) is installed."""
        if 'product.template' not in self.env or 'website' not in self.env:
            return False
        Template = self.env['product.template']
        return (
            'is_published' in Template._fields
            and 'website_url' in Template._fields
            and hasattr(Template, '_get_combination_info')
            and hasattr(Template, '_get_sales_prices')
        )

    @api.model
    def _tool_definitions(self):
        return copy.deepcopy(TOOL_DEFINITIONS)

    @api.model
    def _tool_website(self, livechat_channel):
        """The website showing this live chat channel, else the default one."""
        Website = self.env['website'].sudo()
        website = Website.browse()
        if livechat_channel and 'channel_id' in Website._fields:
            website = Website.search([('channel_id', '=', livechat_channel.id)], order='sequence, id', limit=1)
        return website or Website.get_current_website()

    @api.model
    def _tool_lang(self, website):
        installed = {code for code, _name in self.env['res.lang'].get_installed()}
        if PREFERRED_LANG in installed:
            return PREFERRED_LANG
        return website.default_lang_id.code or 'en_US'

    @api.model
    def _catalog(self, livechat_channel):
        website = self._tool_website(livechat_channel)
        if not website:
            return None
        return _ProductCatalog(self.env, website, self._tool_lang(website))

    @api.model
    def _execute_tool(self, livechat_channel, name, arguments):
        """Run one tool call for the LLM. Never raises.

        Args:
            livechat_channel (im_livechat.channel): channel of the conversation
            name (str): tool (function) name requested by the model
            arguments (str|dict): JSON arguments from the model

        Returns:
            dict: JSON-serialisable tool result (an 'error' key on failure)
        """
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                return {'error': '工具參數不是有效的 JSON。'}
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return {'error': '工具參數格式錯誤。'}
        if name == TOOL_SEARCH_PRODUCTS:
            allowed = ('keyword', 'category', 'max_price')
        elif name == TOOL_PRODUCT_DETAILS:
            allowed = ('product_id', 'name', 'options')
        else:
            return {'error': '沒有這個工具：%s' % name}
        if not self._tools_supported():
            return {'error': '目前無法查詢商品資料。', 'message': NOT_FOUND_MESSAGE}
        kwargs = {key: arguments[key] for key in allowed if arguments.get(key) not in (None, '')}
        for key in ('keyword', 'category', 'name', 'options'):
            if key in kwargs and not isinstance(kwargs[key], str):
                kwargs[key] = str(kwargs[key])
        try:
            with self.env.cr.savepoint():
                catalog = self._catalog(livechat_channel)
                if catalog is None:
                    return {'error': '目前無法查詢商品資料。', 'message': NOT_FOUND_MESSAGE}
                if name == TOOL_SEARCH_PRODUCTS:
                    return catalog.search_products(**kwargs)
                return catalog.get_product_details(**kwargs)
        except Exception:  # noqa: BLE001 - a tool failure must not fail the reply
            _logger.exception("Live chat AI tool %s failed (channel %s)", name,
                              livechat_channel.id if livechat_channel else None)
            return {'error': '查詢商品資料時發生錯誤。', 'message': NOT_FOUND_MESSAGE}
