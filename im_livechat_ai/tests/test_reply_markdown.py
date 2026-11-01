# -*- coding: utf-8 -*-
"""AI replies are rendered as markdown (tools/markdown.py) and stay safe.

Tables, headings, lists, code, bold and links become HTML; only links to the
store's own hosts are clickable; every piece of backend text is escaped and
the result sanitized.
"""

from odoo.tests.common import TransactionCase

from odoo.addons.im_livechat_ai.tools.markdown import markdown_to_html

from .common import TEST_BACKEND, livechat_operator

# A real smart-home advisor reply (n8n, 2026-11-01, shortened): bold, a pipe
# table with markdown links to the shop, an off-site LINE link.
ADVISOR_REPLY = """先以**一間樣品房、一組燈光迴路、房內使用 VRV 冷氣**估算，設備預估總價為 **NT$36,099**：

| 設備 | 數量 | 單價 | 小計 |
|---|---:|---:|---:|
| 房卡訊號輸入：[WOOW MODBUS RTU IO模組 8路](https://aiot.woowtech.io/shop/rtu-io-8ch-woow-modbus-rtu-iomo-zu-8lu-778) | 1 | NT$2,000 | NT$2,000 |
| 燈光控制：[WOOW 智能開關模組（ZigBee）](https://aiot.woowtech.io/shop/smart-switch-module-504) | 1 | NT$1,500 | NT$1,500 |
| 冷氣控制：[WOOW 智能 VRV 冷氣控制器](https://aiot.woowtech.io/shop/vrv-air-conditioner-controller-43) | 1 | NT$20,000 | NT$20,000 |

**估算假設：**現有房卡座可提供乾接點訊號，且冷氣是相容的 VRV 系統；房卡座本身**未計入**。安裝施工另計，實際以客服正式報價為準。

樣品房整合可聯絡 woowtech：LINE [@lwo6431z](https://line.me/R/ti/p/@lwo6431z)、電話 +886 2-2651-9677。**這間房的冷氣是 VRV 系統嗎？**"""

HOSTS = {'aiot.woowtech.io'}


class TestMarkdownRenderer(TransactionCase):

    def render(self, text, hosts=HOSTS):
        return str(markdown_to_html(text, link_hosts=hosts))

    def test_advisor_reply(self):
        html = self.render(ADVISOR_REPLY)
        self.assertIn('<strong>NT$36,099</strong>', html)
        self.assertIn('<strong>一間樣品房、一組燈光迴路、房內使用 VRV 冷氣</strong>', html)
        self.assertIn('<table class="table table-sm table-bordered"><thead><tr><th>設備</th>', html)
        self.assertEqual(html.count('<tr>'), 4)  # header + 3 rows
        self.assertIn('<td style="text-align:right">NT$20,000</td>', html)
        self.assertIn('<a href="https://aiot.woowtech.io/shop/smart-switch-module-504" target="_blank" '
                      'rel="noopener noreferrer">WOOW 智能開關模組（ZigBee）</a>', html)
        self.assertEqual(html.count('<a '), 3)
        # off-site link: its text only
        self.assertNotIn('line.me', html)
        self.assertIn('LINE @lwo6431z、電話', html)
        self.assertNotIn('|---', html)
        self.assertNotIn('**', html)

    def test_headings_lists_code(self):
        html = self.render('### 選購重點\n- 開孔尺寸\n- **色溫**\n\n1. 量尺寸\n2. 選燈\n\n'
                           '輸入 `找真人` 即可。\n```\nif a < b:\n    print("x")\n```')
        self.assertIn('<h4>選購重點</h4>', html)
        self.assertIn('<ul><li>開孔尺寸</li><li><strong>色溫</strong></li></ul>', html)
        self.assertIn('<ol><li>量尺寸</li><li>選燈</li></ol>', html)
        self.assertIn('<code>找真人</code>', html)
        self.assertIn('<pre><code>if a &lt; b:\n    print("x")</code></pre>', html)

    def test_paragraphs_and_line_breaks(self):
        self.assertEqual(self.render('第一行\n第二行\n\n第二段'), '<p>第一行<br>第二行</p><p>第二段</p>')

    def test_links_only_to_allowed_hosts(self):
        html = self.render('[商品](https://aiot.woowtech.io/shop/a) [釣魚](https://evil.example/login) '
                           'https://evil.example/x https://aiot.woowtech.io/shop/b。 [js](javascript:alert(1)) '
                           '[sub](https://x.aiot.woowtech.io/a)')
        self.assertEqual(html.count('<a '), 2)
        self.assertIn('href="https://aiot.woowtech.io/shop/a"', html)
        self.assertIn('href="https://aiot.woowtech.io/shop/b"', html)  # trailing punctuation not in the URL
        self.assertIn('釣魚', html)
        self.assertIn('https://evil.example/x', html)  # plain text
        self.assertNotIn('href="https://evil', html)
        self.assertNotIn('href="javascript', html)
        self.assertNotIn('href="https://x.aiot', html)
        self.assertEqual(self.render('[a](https://aiot.woowtech.io/x)', hosts=()).count('<a '), 0)

    def test_html_is_escaped_everywhere(self):
        html = self.render('<script>alert(1)</script>\n\n| <img src=x onerror=alert(1)> | b |\n|---|---|\n'
                           '| `<b>` | **<i>x</i>** |\n\n### <svg onload=alert(1)>\n- <iframe src=x>')
        for bad in ('<script', '<img', '<svg', '<iframe', '<i>', '<b>'):
            self.assertNotIn(bad, html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('<code>&lt;b&gt;</code>', html)

    def test_empty(self):
        self.assertEqual(self.render(''), '')
        self.assertEqual(self.render('   \n'), '')


class TestBotMessageMarkdown(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://aiot.woowtech.io')
        cls.channel = cls.env['im_livechat.channel'].create({**TEST_BACKEND, 'name': 'MD', 'ai_enabled': True})
        cls.session = cls.env['discuss.channel'].create({
            'name': 'MD Session', 'channel_type': 'livechat',
            'livechat_operator_id': livechat_operator(cls.env).id, 'livechat_channel_id': cls.channel.id,
        })

    def test_posted_advisor_reply(self):
        message = self.channel._create_bot_message(self.env, self.session, ADVISOR_REPLY)
        body = str(message.body)
        self.assertEqual(message.author_id, self.channel.ai_bot_partner_id)
        self.assertIn('<table', body)
        self.assertIn('<th>設備</th>', body)
        self.assertIn('<strong>NT$36,099</strong>', body)
        self.assertIn('href="https://aiot.woowtech.io/shop/rtu-io-8ch-woow-modbus-rtu-iomo-zu-8lu-778"', body)
        self.assertNotIn('line.me', body)
        self.assertNotIn('|---', body)
