# -*- coding: utf-8 -*-
"""Package E (P1-8): product lookup tools through OpenAI-style tool calling.

All HTTP is mocked; no real key is used. The LLM responses below request the
tools the way LiteLLM returns them (choices[0].message.tool_calls). Products
are seeded with the production variant structure of the standard recessed
light (base 650 + 雙色溫 150 + ZIGBEE電源 720 = 1520).
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import html2plaintext, mute_logger

from odoo.addons.im_livechat_ai.models.im_livechat_channel import (
    LLM_MAX_TOOL_CALLS_PER_ROUND,
    LLM_MAX_TOOL_ROUNDS,
    PRODUCT_GUARD_NO_TOOLS,
    PRODUCT_GUARD_WITH_TOOLS,
    TOOL_ROUND_LIMIT_NOTE,
)
from odoo.addons.im_livechat_ai.models.livechat_ai_tools import (
    NOT_FOUND_MESSAGE,
    SEARCH_RESULT_LIMIT,
    TOOL_PRODUCT_DETAILS,
    TOOL_SEARCH_PRODUCTS,
)

from .common import livechat_operator, setup_litellm

POST = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.requests.post'
SLEEP = 'odoo.addons.im_livechat_ai.models.im_livechat_channel.time.sleep'
BASE_URL = 'https://shop.example.test'


def _response(message, usage=None):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': dict(message, role='assistant'), 'finish_reason': 'stop'}],
        'usage': usage or {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18},
    }
    response.raise_for_status = MagicMock()
    return response


def _answer(content):
    return _response({'content': content})


_CALL_IDS = {'n': 0}


def _reset_call_ids():
    _CALL_IDS['n'] = 0


def _next_id():
    _CALL_IDS['n'] += 1
    return _CALL_IDS['n']


def _tool_response(*calls, content=None):
    """calls: (name, arguments dict or raw string); every tool call id is unique."""
    tool_calls = []
    for index, (name, arguments) in enumerate(calls):
        tool_calls.append({
            'index': index,
            'id': f'call_{name}_{index}_{_next_id()}',
            'type': 'function',
            'function': {
                'name': name,
                'arguments': arguments if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False),
            },
        })
    message = {'tool_calls': tool_calls}
    if content is not None:
        message['content'] = content
    return _response(message)


def _payloads(mock_post):
    return [call.kwargs['json'] for call in mock_post.call_args_list]


def _tool_results(payload):
    """{tool_call_id: parsed result} of the tool messages sent in a payload."""
    return {
        m['tool_call_id']: json.loads(m['content'])
        for m in payload['messages'] if m.get('role') == 'tool'
    }


@tagged('post_install', '-at_install')
class ProductToolsCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.env['im_livechat.ai.tools']._tools_supported():
            raise unittest.SkipTest('website_sale is not installed: product tools are unavailable')
        setup_litellm(cls.env)
        cls.env['res.lang']._activate_lang('zh_TW')
        cls.env['ir.config_parameter'].sudo().set_param('web.base.url', BASE_URL + '/')

        cls.channel = cls.env['im_livechat.channel'].create({
            'name': 'Product Channel',
            'ai_enabled': True,
            'ai_model': 'chatgpt-pool/gpt-6-sol',
            'ai_max_retries': 2,
            'ai_system_prompt': '你是 woowtech 的客服助理。',
        })
        cls.bot = cls.channel._get_or_create_bot_partner()
        cls.website = cls.env['website'].search([], order='id', limit=1)
        if 'channel_id' in cls.website._fields:  # website_livechat
            cls.website.channel_id = cls.channel
        cls.other_website = cls.env['website'].create({'name': 'Other Shop'})

        Category = cls.env['product.public.category']
        cls.cat_light = Category.create({'name': 'Smart Lighting'})
        cls.cat_light.with_context(lang='zh_TW').name = '智能照明'
        cls.cat_curtain = Category.create({'name': 'Smart Curtains'})
        cls.cat_curtain.with_context(lang='zh_TW').name = '智能窗簾'

        Attr = cls.env['product.attribute']
        cls.attr_spec = Attr.create({'name': 'Light Fixture Specification', 'create_variant': 'always'})
        cls.attr_power = Attr.create({'name': '電源配置', 'create_variant': 'always'})
        cls.attr_size = Attr.create({'name': 'Size', 'create_variant': 'always'})
        cls.attr_addon = Attr.create({'name': 'Add-on', 'create_variant': 'no_variant'})

        def values(attr, names):
            return cls.env['product.attribute.value'].create([
                {'attribute_id': attr.id, 'name': name} for name in names
            ])

        spec = values(cls.attr_spec, ['單色', '雙色溫'])
        power = values(cls.attr_power, ['一般電源', 'ZIGBEE電源'])
        size = values(cls.attr_size, ['標準 Φ75mm', '細邊 Φ95mm'])
        addon = values(cls.attr_addon, ['+1m', '+2m'])

        def template(name, zh_name, price, **vals):
            vals.setdefault('is_published', True)
            vals.setdefault('sale_ok', True)
            tpl = cls.env['product.template'].create(dict({
                'name': name,
                'list_price': price,
                'type': 'consu',
                'taxes_id': [(5, 0, 0)],
            }, **vals))
            if zh_name:
                tpl.with_context(lang='zh_TW').name = zh_name
            return tpl

        cls.light = template(
            'WOOW Smart Standard Recessed Light', 'WOOW 智能標準崁燈', 650.0,
            description_sale='尺寸 : Φ85 x 95 mm\n開孔 : Φ75 mm',
            public_categ_ids=[(6, 0, [cls.cat_light.id])],
            attribute_line_ids=[
                (0, 0, {'attribute_id': cls.attr_spec.id, 'value_ids': [(6, 0, spec.ids)]}),
                (0, 0, {'attribute_id': cls.attr_power.id, 'value_ids': [(6, 0, power.ids)]}),
                (0, 0, {'attribute_id': cls.attr_size.id, 'value_ids': [(6, 0, size.ids)]}),
            ],
        )
        extras = {'雙色溫': 150.0, 'ZIGBEE電源': 720.0, '細邊 Φ95mm': 130.0}
        for ptav in cls.light.attribute_line_ids.product_template_value_ids:
            if ptav.name in extras:
                ptav.price_extra = extras[ptav.name]

        cls.curtain = template(
            'WOOW Smart Curtain Track Motor', 'WOOW 智能窗簾馬達', 11999.0,
            public_categ_ids=[(6, 0, [cls.cat_curtain.id])],
            attribute_line_ids=[
                (0, 0, {'attribute_id': cls.attr_addon.id, 'value_ids': [(6, 0, addon.ids)]}),
            ],
        )
        addon_extras = {'+1m': 300.0, '+2m': 600.0}
        for ptav in cls.curtain.attribute_line_ids.product_template_value_ids:
            ptav.price_extra = addon_extras[ptav.name]

        # Never returned: unpublished, not sellable, other website, archived.
        cls.unpublished = template('WOOW Secret Recessed Light', '秘密崁燈', 99.0, is_published=False)
        cls.not_sellable = template('WOOW Display Recessed Light', '展示用崁燈', 99.0, sale_ok=False)
        cls.other_site = template('WOOW Other Site Recessed Light', '他站崁燈', 99.0,
                                  website_id=cls.other_website.id)
        cls.archived = template('WOOW Archived Recessed Light', '停售崁燈', 99.0, active=False)
        cls.hidden = cls.unpublished | cls.not_sellable | cls.other_site | cls.archived

        cls.bulbs = cls.env['product.template']
        for i in range(10):
            cls.bulbs |= template(f'Test Bulb {i}', f'測試燈泡 {i}', 100.0 + i,
                                  public_categ_ids=[(6, 0, [cls.cat_light.id])])

        cls.operator = livechat_operator(cls.env)
        cls.visitor = cls.env['res.partner'].create({'name': 'Product Visitor'})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'Product Session',
            'channel_type': 'livechat',
            'livechat_operator_id': cls.operator.id,
            'livechat_channel_id': cls.channel.id,
        })
        cls.Tools = cls.env['im_livechat.ai.tools']

    def setUp(self):
        super().setUp()
        _reset_call_ids()

    def _tool(self, tool_name, **arguments):
        return self.Tools._execute_tool(self.channel, tool_name, json.dumps(arguments, ensure_ascii=False))

    def _variant(self, *value_names):
        return self.light.product_variant_ids.filtered(
            lambda v: set(v.product_template_attribute_value_ids.mapped('name')) == set(value_names)
        )

    def _website_price(self, variant):
        """What the shop shows to a public visitor for this variant."""
        public = self.website.user_id
        variant = variant.with_user(public).sudo().with_context(website_id=self.website.id)
        return variant._get_combination_info_variant()['price']

    def _post(self, body):
        return self.session.message_post(
            body=body, author_id=self.visitor.id,
            message_type='comment', subtype_xmlid='mail.mt_comment',
        )

    def _run(self, body='標準崁燈 Φ75 雙色溫 ZigBee 多少錢？'):
        self._post(body)
        job = self.env['im_livechat.ai.job'].search(
            [('discuss_channel_id', '=', self.session.id)], order='id desc', limit=1)
        job._run_job(commit=False)
        return job

    def _last_reply(self):
        return html2plaintext(self._bot_messages()[-1].body).strip()

    def _bot_messages(self):
        return self.env['mail.message'].search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', self.session.id),
            ('author_id', '=', self.bot.id),
        ], order='id')


class TestProductTools(ProductToolsCase):
    """The ORM tools: published products only, website prices, URLs."""

    def test_search_returns_zh_name_price_range_and_absolute_url(self):
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='崁燈')
        ids = [r['product_id'] for r in result['results']]
        self.assertEqual(ids, [self.light.id])
        item = result['results'][0]
        self.assertEqual(item['name'], 'WOOW 智能標準崁燈')
        self.assertEqual(item['price_min'], 650)
        self.assertEqual(item['price_max'], 650 + 150 + 720 + 130)
        self.assertEqual(item['variant_count'], 8)
        self.assertEqual(item['currency'], self.website.currency_id.name)
        self.assertEqual(item['categories'], ['智能照明'])
        self.assertIn('Φ85', item['short_description'])
        self.assertTrue(item['url'].startswith(BASE_URL + '/shop/'), item['url'])
        self.assertTrue(item['url'].endswith('-%s' % self.light.id), item['url'])

    def test_search_multi_word_chinese_query(self):
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='標準崁燈 Φ75 雙色溫 ZigBee')
        self.assertEqual(result['results'][0]['product_id'], self.light.id)

    def test_search_english_name(self):
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='recessed light')
        self.assertEqual(result['results'][0]['product_id'], self.light.id)

    def test_search_never_returns_hidden_products(self):
        for keyword in ('崁燈', 'Recessed', '秘密', '展示用', '他站', '停售', 'WOOW'):
            result = self._tool(TOOL_SEARCH_PRODUCTS, keyword=keyword)
            ids = {r['product_id'] for r in result['results']}
            self.assertFalse(ids & set(self.hidden.ids), keyword)
        hidden = self._tool(TOOL_SEARCH_PRODUCTS, keyword='秘密崁燈')
        self.assertNotIn(self.unpublished.id, [r['product_id'] for r in hidden['results']])

    def test_search_hides_product_unpublished_later(self):
        self.light.is_published = False
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='崁燈')
        self.assertEqual(result['results'], [])
        self.assertEqual(result['message'], NOT_FOUND_MESSAGE)

    def test_search_is_limited_to_eight(self):
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='燈泡')
        self.assertEqual(result['count'], SEARCH_RESULT_LIMIT)
        self.assertEqual(len(result['results']), 8)
        self.assertTrue(set(r['product_id'] for r in result['results']) <= set(self.bulbs.ids))

    def test_search_by_category_and_max_price(self):
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='', category='窗簾')
        self.assertEqual([r['product_id'] for r in result['results']], [self.curtain.id])
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='馬達', category='窗簾', max_price=5000)
        self.assertEqual(result['results'], [])
        self.assertEqual(result['message'], NOT_FOUND_MESSAGE)
        self.assertTrue(any('5000' in n for n in result['notes']), result)
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='燈', category='照明', max_price=103)
        self.assertEqual(sorted(r['product_id'] for r in result['results']), sorted(self.bulbs[:4].ids))

    def test_search_unknown_category_falls_back_to_keyword(self):
        result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='崁燈', category='不存在的分類')
        self.assertEqual([r['product_id'] for r in result['results']], [self.light.id])
        self.assertTrue(result['notes'])

    def test_details_variant_prices_equal_website_prices(self):
        result = self._tool(TOOL_PRODUCT_DETAILS, product_id=self.light.id)
        self.assertEqual(result['product_id'], self.light.id)
        self.assertEqual(result['name'], 'WOOW 智能標準崁燈')
        self.assertEqual(result['variant_count'], 8)
        self.assertEqual(len(result['variants']), 8)
        by_id = {v.id: v for v in self.light.product_variant_ids}
        for item in result['variants']:
            variant = by_id[item['variant_id']]
            self.assertEqual(item['price'], variant.lst_price)
            self.assertEqual(item['price'], self._website_price(variant))
            self.assertEqual(set(item['options'].values()),
                             set(variant.product_template_attribute_value_ids.mapped('name')))
            self.assertTrue(item['url'].startswith(BASE_URL + '/shop/'))
            self.assertIn('#attribute_values=', item['url'])
        prices = [v['price'] for v in result['variants']]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual({a['name'] for a in result['attributes']},
                         {'Light Fixture Specification', '電源配置', 'Size'})

    def test_details_options_select_the_seeded_combination(self):
        result = self._tool(TOOL_PRODUCT_DETAILS, product_id=self.light.id,
                            options='雙色溫, ZIGBEE電源, 標準 Φ75mm')
        self.assertEqual(result['matched_variant_count'], 1)
        item = result['variants'][0]
        variant = self._variant('雙色溫', 'ZIGBEE電源', '標準 Φ75mm')
        self.assertEqual(item['variant_id'], variant.id)
        self.assertEqual(item['price'], 1520)
        self.assertEqual(item['price'], variant.lst_price)
        self.assertEqual(item['price'], self._website_price(variant))

    def test_details_options_case_and_unknown_values(self):
        result = self._tool(TOOL_PRODUCT_DETAILS, product_id=self.light.id,
                            options='zigbee電源, 雙色溫, 紫色')
        self.assertEqual(result['matched_variant_count'], 2)
        self.assertEqual({v['price'] for v in result['variants']}, {1520, 1650})
        self.assertTrue(any('紫色' in n for n in result['notes']))

    def test_details_by_name(self):
        result = self._tool(TOOL_PRODUCT_DETAILS, name='標準崁燈')
        self.assertEqual(result['product_id'], self.light.id)

    def test_details_never_returns_hidden_products(self):
        for template in self.hidden:
            result = self._tool(TOOL_PRODUCT_DETAILS, product_id=template.id)
            self.assertIn('error', result, template.name)
            self.assertNotIn('variants', result)
        for name in ('秘密崁燈', '展示用崁燈', '他站崁燈', '停售崁燈'):
            result = self._tool(TOOL_PRODUCT_DETAILS, name=name)
            self.assertNotIn(result.get('product_id'), self.hidden.ids, name)
            self.assertFalse({m['product_id'] for m in result.get('other_matches', [])} & set(self.hidden.ids))

    def test_details_optional_attribute_extra_price(self):
        result = self._tool(TOOL_PRODUCT_DETAILS, product_id=self.curtain.id)
        self.assertEqual(result['variants'][0]['price'], 11999)
        self.assertEqual(result['optional_attributes'], [{
            'name': 'Add-on',
            'values': [{'name': '+1m', 'extra_price': 300}, {'name': '+2m', 'extra_price': 600}],
        }])

    def test_bad_arguments_and_unknown_tool(self):
        self.assertIn('error', self.Tools._execute_tool(self.channel, TOOL_SEARCH_PRODUCTS, '{not json'))
        self.assertIn('error', self.Tools._execute_tool(self.channel, TOOL_SEARCH_PRODUCTS, '[1, 2]'))
        self.assertIn('error', self.Tools._execute_tool(self.channel, 'delete_products', '{}'))
        self.assertIn('error', self._tool(TOOL_SEARCH_PRODUCTS, keyword=''))
        self.assertIn('error', self._tool(TOOL_PRODUCT_DETAILS))

    @mute_logger('odoo.addons.im_livechat_ai.models.livechat_ai_tools')
    def test_tool_exception_becomes_error_result(self):
        with patch.object(type(self.Tools), '_catalog', side_effect=RuntimeError('boom')):
            result = self._tool(TOOL_SEARCH_PRODUCTS, keyword='崁燈')
        self.assertEqual(result['message'], NOT_FOUND_MESSAGE)
        self.assertIn('error', result)


class TestProductGuard(ProductToolsCase):
    """The fixed product rules and the per-channel toggle."""

    def test_guard_always_in_system_prompt(self):
        self._post('崁燈多少錢？')
        messages = self.session._build_llm_messages(self.channel)
        self.assertEqual(messages[0]['role'], 'system')
        self.assertTrue(messages[0]['content'].startswith('你是 woowtech 的客服助理。'))
        self.assertTrue(messages[0]['content'].endswith(PRODUCT_GUARD_WITH_TOOLS))
        self.assertIn('絕對不可編造價格', PRODUCT_GUARD_WITH_TOOLS)

    def test_toggle_default_on_and_off(self):
        self.assertTrue(self.env['im_livechat.channel'].create({'name': 'New'}).ai_product_lookup)
        self.assertEqual([t['function']['name'] for t in self.channel._ai_tool_definitions()],
                         [TOOL_SEARCH_PRODUCTS, TOOL_PRODUCT_DETAILS])
        self.channel.ai_product_lookup = False
        self.assertEqual(self.channel._ai_tool_definitions(), [])
        self._post('崁燈多少錢？')
        messages = self.session._build_llm_messages(self.channel)
        self.assertTrue(messages[0]['content'].endswith(PRODUCT_GUARD_NO_TOOLS))

    def test_no_tools_without_website_sale(self):
        with patch.object(type(self.Tools), '_tools_supported', return_value=False):
            self.assertEqual(self.channel._ai_tool_definitions(), [])
            self.assertEqual(self.channel._ai_product_guard(), PRODUCT_GUARD_NO_TOOLS)


class TestToolLoop(ProductToolsCase):
    """Mocked LiteLLM responses that request the tools."""

    @patch(POST)
    def test_search_then_details_then_answer(self, mock_post):
        final = '標準崁燈 Φ75 雙色溫 ZigBee 的官網售價是 NT$1,520。'
        mock_post.side_effect = [
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': '標準崁燈'})),
            _tool_response((TOOL_PRODUCT_DETAILS, {
                'product_id': self.light.id, 'options': '雙色溫, ZIGBEE電源, 標準 Φ75mm',
            })),
            _answer(final),
        ]
        job = self._run()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.llm_calls, 3)
        self.assertEqual(self._last_reply(), final)

        first, second, third = _payloads(mock_post)
        for payload in (first, second, third):
            self.assertEqual([t['function']['name'] for t in payload['tools']],
                             [TOOL_SEARCH_PRODUCTS, TOOL_PRODUCT_DETAILS])
            self.assertEqual(payload['tool_choice'], 'auto')
            self.assertTrue(payload['messages'][0]['content'].endswith(PRODUCT_GUARD_WITH_TOOLS))

        # Round 1: the assistant tool call is echoed back with the result.
        assistant = second['messages'][-2]
        self.assertEqual(assistant['role'], 'assistant')
        self.assertEqual(assistant['tool_calls'][0]['function']['name'], TOOL_SEARCH_PRODUCTS)
        self.assertEqual(json.loads(assistant['tool_calls'][0]['function']['arguments']),
                         {'keyword': '標準崁燈'})
        self.assertNotIn('index', assistant['tool_calls'][0])
        search = list(_tool_results(second).values())[0]
        self.assertEqual([r['product_id'] for r in search['results']], [self.light.id])
        self.assertEqual(search['results'][0]['price_min'], 650)

        # Round 2: the details of the chosen combination, with the seeded price.
        details = list(_tool_results(third).values())[1]
        variant = self._variant('雙色溫', 'ZIGBEE電源', '標準 Φ75mm')
        self.assertEqual(details['variants'], [{
            'variant_id': variant.id,
            'options': {'Light Fixture Specification': '雙色溫', '電源配置': 'ZIGBEE電源', 'Size': '標準 Φ75mm'},
            'price': 1520,
            'url': BASE_URL + variant.with_context(lang='zh_TW').website_url,
        }])
        # Both tool results are still in the history of the last call.
        self.assertEqual(len(_tool_results(third)), 2)

        logs = self.env['llm.api.log'].search([('discuss_channel_id', '=', self.session.id)], order='id')
        self.assertEqual(logs.mapped('status'), ['success'] * 3)
        self.assertEqual(logs.mapped('tool_names'), [TOOL_SEARCH_PRODUCTS, TOOL_PRODUCT_DETAILS, False])
        self.assertEqual(logs.mapped('total_tokens'), [18, 18, 18])
        self.assertTrue(all(log.response_time is not None for log in logs))
        # Conversation content is not stored by default (package F).
        self.assertFalse(any(logs.mapped('request_payload')))
        self.assertFalse(any(logs.mapped('response_payload')))

    @patch(POST)
    def test_three_round_cap_forces_final_answer(self, mock_post):
        mock_post.side_effect = [
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': f'崁燈 {i}'})) for i in range(LLM_MAX_TOOL_ROUNDS)
        ] + [_tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': 'again'}), content='請留下聯絡方式，客服會報價。')]
        job = self._run()
        self.assertEqual(job.state, 'done')
        self.assertEqual(mock_post.call_count, LLM_MAX_TOOL_ROUNDS + 1)
        self.assertEqual(job.llm_calls, 4)
        payloads = _payloads(mock_post)
        self.assertEqual([p['tool_choice'] for p in payloads], ['auto', 'auto', 'auto', 'none'])
        last = payloads[-1]
        self.assertEqual(last['messages'][-1], {'role': 'system', 'content': TOOL_ROUND_LIMIT_NOTE})
        self.assertEqual(len(_tool_results(last)), LLM_MAX_TOOL_ROUNDS)
        # The tool call in the forced final answer is ignored, its text is used.
        self.assertEqual(self._last_reply(), '請留下聯絡方式，客服會報價。')

    @patch(SLEEP)
    @patch(POST)
    @mute_logger('odoo.addons.im_livechat_ai.models.im_livechat_channel')
    def test_tool_calls_without_text_on_final_call_fail_cleanly(self, mock_post, mock_sleep):
        self.channel.ai_max_retries = 0
        mock_post.side_effect = [
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': '崁燈'})) for _i in range(LLM_MAX_TOOL_ROUNDS + 1)
        ]
        job = self._run()
        self.assertEqual(mock_post.call_count, LLM_MAX_TOOL_ROUNDS + 1)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self._last_reply(), self.channel.ai_error_message)

    @patch(POST)
    def test_model_without_tool_calls_answers_directly(self, mock_post):
        mock_post.return_value = _answer('<think>先想一下</think>您好，請問要查哪一款？')
        job = self._run('你好')
        self.assertEqual(job.state, 'done')
        self.assertEqual(mock_post.call_count, 1)
        self.assertIn('tools', _payloads(mock_post)[0])
        self.assertEqual(self._last_reply(), '您好，請問要查哪一款？')

    @patch(POST)
    def test_toggle_off_sends_no_tools(self, mock_post):
        self.channel.ai_product_lookup = False
        mock_post.return_value = _answer('價格請留下聯絡方式，由客服報價。')
        self._run()
        payload = _payloads(mock_post)[0]
        self.assertNotIn('tools', payload)
        self.assertNotIn('tool_choice', payload)
        self.assertTrue(payload['messages'][0]['content'].endswith(PRODUCT_GUARD_NO_TOOLS))

    @patch(POST)
    def test_think_blocks_stripped_after_tools(self, mock_post):
        mock_post.side_effect = [
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': '崁燈'}), content='<think>查一下</think>'),
            _answer('<think>價格 650</think>起價 NT$650。'),
        ]
        self._run()
        self.assertEqual(self._last_reply(), '起價 NT$650。')

    @patch(POST)
    def test_unknown_tool_and_bad_arguments_get_error_results(self, mock_post):
        mock_post.side_effect = [
            _tool_response(('drop_all_products', {}), (TOOL_SEARCH_PRODUCTS, '{"keyword": ')),
            _answer('抱歉，目前查不到，請留下聯絡方式。'),
        ]
        job = self._run()
        self.assertEqual(job.state, 'done')
        results = _tool_results(_payloads(mock_post)[1])
        self.assertEqual(len(results), 2)
        self.assertTrue(all('error' in r for r in results.values()))

    @patch(POST)
    def test_tool_calls_beyond_per_round_limit_are_not_run(self, mock_post):
        calls = [(TOOL_SEARCH_PRODUCTS, {'keyword': f'燈泡 {i}'}) for i in range(LLM_MAX_TOOL_CALLS_PER_ROUND + 2)]
        mock_post.side_effect = [_tool_response(*calls), _answer('好的')]
        real = type(self.Tools)._execute_tool
        executed = []

        def spy(model, channel, name, arguments):
            executed.append(name)
            return real(model, channel, name, arguments)

        with patch.object(type(self.Tools), '_execute_tool', spy):
            self._run()
        self.assertEqual(len(executed), LLM_MAX_TOOL_CALLS_PER_ROUND)
        results = _tool_results(_payloads(mock_post)[1])
        self.assertEqual(len(results), LLM_MAX_TOOL_CALLS_PER_ROUND + 2)
        self.assertEqual(sum(1 for r in results.values() if 'error' in r), 2)

    @patch(SLEEP)
    @patch(POST)
    @mute_logger('odoo.addons.im_livechat_ai.models.im_livechat_channel')
    def test_retry_inside_tool_loop_shares_the_budget(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            requests.exceptions.Timeout('slow'),
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': '崁燈'})),
            requests.exceptions.ConnectionError('down'),
            _answer('起價 NT$650。'),
        ]
        job = self._run()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.llm_calls, 4)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [3, 10])
        logs = self.env['llm.api.log'].search([('discuss_channel_id', '=', self.session.id)], order='id')
        self.assertEqual(logs.mapped('status'), ['retry', 'success', 'retry', 'success'])

    @patch(SLEEP)
    @patch(POST)
    @mute_logger('odoo.addons.im_livechat_ai.models.im_livechat_channel')
    def test_retry_budget_exhausted_in_tool_loop(self, mock_post, mock_sleep):
        self.channel.ai_max_retries = 1
        mock_post.side_effect = [
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': '崁燈'})),
            requests.exceptions.Timeout('slow'),
            requests.exceptions.Timeout('slow'),
        ]
        job = self._run()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.llm_calls, 3)
        logs = self.env['llm.api.log'].search([('discuss_channel_id', '=', self.session.id)], order='id')
        self.assertEqual(logs.mapped('status'), ['success', 'retry', 'error'])

    @patch(POST)
    def test_payload_logged_only_when_enabled(self, mock_post):
        setup_litellm(self.env, log_payload=True)
        mock_post.side_effect = [
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': '崁燈'})),
            _answer('起價 NT$650。'),
        ]
        self._run()
        logs = self.env['llm.api.log'].search([('discuss_channel_id', '=', self.session.id)], order='id')
        self.assertEqual(len(logs), 2)
        sent = json.loads(logs[1].request_payload)
        self.assertEqual(sent[-1]['role'], 'tool')
        self.assertIn('WOOW 智能標準崁燈', sent[-1]['content'])
        self.assertIn('tool_calls', logs[0].response_payload)

    @patch(POST)
    def test_markdown_product_link_is_posted_as_link(self, mock_post):
        url = BASE_URL + self.light.with_context(lang='zh_TW').website_url
        mock_post.side_effect = [
            _tool_response((TOOL_SEARCH_PRODUCTS, {'keyword': '崁燈'})),
            _answer(f'起價 **NT$650**：[WOOW 智能標準崁燈]({url})'),
        ]
        job = self._run()
        body = job.reply_message_id.body
        self.assertIn('<strong>NT$650</strong>', body)
        self.assertIn(f'href="{url}"', body)
        self.assertIn('>WOOW 智能標準崁燈</a>', body)
        self.assertNotIn('](', body)


class TestReplyFormatting(TransactionCase):
    """Markdown links / URLs / bold in AI text become safe HTML."""

    def _fmt(self, text):
        return str(self.env['im_livechat.channel']._format_reply_line(text))

    def test_markdown_link_bold_and_bare_url(self):
        html = self._fmt('售價 **NT$1,520**，[崁燈](https://shop.test/shop/a-17#attribute_values=1,2)。')
        self.assertEqual(html, '售價 <strong>NT$1,520</strong>，<a href="https://shop.test/shop/a-17'
                               '#attribute_values=1,2" target="_blank" rel="noopener noreferrer">崁燈</a>。')
        html = self._fmt('網址https://shop.test/p?a=1&b=2，謝謝')
        self.assertEqual(html, '網址<a href="https://shop.test/p?a=1&amp;b=2" target="_blank" '
                               'rel="noopener noreferrer">https://shop.test/p?a=1&amp;b=2</a>，謝謝')
        self.assertTrue(self._fmt('see https://shop.test/a.').endswith('</a>.'))

    def test_unsafe_links_stay_text(self):
        html = self._fmt('[x](javascript:alert(1)) <script>alert(1)</script>')
        self.assertNotIn('<a', html)
        self.assertNotIn('<script>', html)
        html = self._fmt('[a"><img src=x>](https://e.test/"onmouseover=1)')
        self.assertNotIn('<img', html)
        self.assertNotIn('"onmouseover', html)
