"""drpy 单文件源适配层：规则解析 / 选择器 / 播放页解析 / 分发接线。

这些用例全部离线跑：网络请求一律用 httpx.MockTransport 或直接喂 HTML 字符串。
"""
import _bootstrap  # noqa: F401 - 统一临时 state 目录与 sys.path

import json
import unittest

import httpx

import cms
import config
import drpy

SAMPLE = r"""
/*
 * 测试用 drpy 源：故意混上注释、正则字面量与 backtick 代码块，
 * 这三样是规则解析最容易崩的地方。
 */
var rule = {
    title: '样例站[磁]',
    host: 'https://demo.invalid',
    url: '/fyclassfyfilter/index_fypage.html[/fyclassfyfilter/index.html]',
    searchUrl: '/e/search/index.php#show=title&keyboard=**;post',
    headers: {'User-Agent': 'MOBILE_UA'},
    timeout: 5000,
    class_parse: '#menus&&li:gt(1);a&&Text;a&&href;.*/(.*)/',
    cate_exclude: '旧版',
    一级: '#post_container&&li;h2&&Text;img&&src;.info_date&&Text;a&&href',
    二级: {
        "title": "#content&&h1&&Text",
        "img": "#post_content&&img&&src",
        "desc": ";;;#post_content&&p:eq(0)&&Text",
        "tabs": `js:
            TABS = ["磁力"];
            let tabs = pdfa(html, '#content&&h3:not(:contains(网盘))');
        `,
        "lists": "js: LISTS = [];",
    },
    搜索: '*',
    lazy: $js.toString(function () {
        var m = html.match(/player_data\s*=\s*(\{[\s\S]*?\})/);
        return m ? m[1] : '';
    }),
}
"""

BILLI = """
<html><body>
<ul id="menus">
  <li><a href="/">首页</a></li>
  <li><a href="/old/">旧版</a></li>
  <li><a href="/dianshiju/">电视剧</a></li>
  <li><a href="/dianying/">电影</a></li>
</ul>
<div id="post_container">
  <li><h2><a href="/dianshiju/1.html">第一部</a></h2>
      <img src="/p1.jpg"><span class="info_date">2026-11-20</span></li>
  <li><h2><a href="/dianshiju/2.html">第二部</a></h2>
      <img src="/p2.jpg"><span class="info_date">2026-11-19</span></li>
</div>
</body></html>
"""

DETAIL_JS = """
<html><body><div id="content"><h1>测试剧</h1>
<h3>播放地址（无插件 极速播放）</h3>
<div class="context">
  <div class="widget">帮助：不推荐使用迅雷下载</div>
  <div class="widget">播放地址（无插件 极速播放）
    <a href="/e/play/?p1=0">第01集</a><a href="/e/play/?p1=1">第02集</a></div>
  <div class="widget">播放地址三
    <a href="magnet:?xt=urn:btih:abc">磁力01</a></div>
  <div class="widget">播放地址四
    <a href="/e/play/?p4=0">第01集</a></div>
</div></div></body></html>
"""

DETAIL_MULTI = """
<html><body>
<h3>视频列表</h3>
<div id="playlist1"><ul><li><a href="/v/1-1-1.html">第1集</a></li>
  <li><a href="/v/1-1-2.html">第2集</a></li></ul></div>
<div id="playlist2"><ul><li><a href="/v/1-2-1.html">第1集</a></li></ul></div>
</body></html>
"""


class RuleParseTest(unittest.TestCase):
    def test_comments_regex_and_template_blocks(self):
        rule = drpy.parse_rule(SAMPLE)
        self.assertEqual(rule["title"], "样例站[磁]")
        self.assertEqual(rule["host"], "https://demo.invalid")
        # backtick 代码块整体保留，不参与求值
        self.assertTrue(rule["二级"]["tabs"].strip().startswith("js:"))
        self.assertIn("player_data", rule["lazy"])
        self.assertEqual(rule["timeout"], 5000)

    def test_missing_rule_is_rejected(self):
        with self.assertRaises(drpy.JsRuleError):
            drpy.parse_rule("console.log('no rule here')")

    def test_rule_without_host_is_rejected(self):
        with self.assertRaises(drpy.JsRuleError):
            drpy.parse_rule("var rule = {title: 'x', url: '/'}")


class TemplateTest(unittest.TestCase):
    def test_first_page_uses_bracket_address(self):
        tpl = "/fyclassfyfilter/index_fypage.html[/fyclassfyfilter/index.html]"
        self.assertEqual(drpy._expand(tpl, cls="dianshiju", page=1),
                         "/dianshiju/index.html")
        self.assertEqual(drpy._expand(tpl, cls="dianshiju", page=3),
                         "/dianshiju/index_3.html")

    def test_keyword_is_quoted(self):
        got = drpy._expand("/search?wd=**", keyword="庆 余年")
        self.assertIn("%E5%BA%86", got)
        self.assertNotIn(" ", got)


class SelectorTest(unittest.TestCase):
    def setUp(self):
        self.tree = drpy._parser(BILLI)

    def test_gt_skips_first_and_exclude_filters(self):
        rule = drpy.rule_of({"rule": SAMPLE})
        rows = drpy._classes(BILLI, rule, "https://demo.invalid/")
        names = [r["name"] for r in rows]
        self.assertEqual(names, ["电视剧", "电影"])
        self.assertEqual(rows[0]["id"], "dianshiju")

    def test_eq_pseudo(self):
        html = ("<div class='a'>一</div><div class='a'>二</div>"
                "<div class='a'>三</div>")
        tree = drpy._parser(html)
        self.assertEqual(drpy.value(tree.root, ".a:eq(1)&&Text"), "二")
        self.assertEqual(drpy.value(tree.root, ".a:gt(1)&&Text"), "三")
        self.assertEqual(drpy.value(tree.root, ".a:lt(1)&&Text"), "一")

    def test_contains_and_not_contains(self):
        html = ("<h3>播放地址（无插件）</h3><h3>网盘下载</h3>")
        tree = drpy._parser(html)
        self.assertEqual(
            drpy.value(tree.root, "h3:contains(网盘)&&Text"), "网盘下载")
        self.assertEqual(
            drpy.value(tree.root, "h3:not(:contains(网盘))&&Text"),
            "播放地址（无插件）")

    def test_multi_fallback_with_semicolon(self):
        html = "<h1 class='t'>真标题</h1>"
        tree = drpy._parser(html)
        self.assertEqual(drpy.pick(tree.root, ".nope&&Text;h1.t&&Text"), "真标题")

    def test_list_rows(self):
        rule = drpy.rule_of({"rule": SAMPLE})
        rows = drpy._rows(BILLI, rule["一级"], "https://demo.invalid/dianshiju/")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "第一部")
        self.assertEqual(rows[0]["poster"], "https://demo.invalid/p1.jpg")
        self.assertEqual(rows[0]["remark"], "2026-11-20")
        self.assertEqual(rows[0]["url"], "https://demo.invalid/dianshiju/1.html")


class DetailTest(unittest.TestCase):
    def test_js_blocks_fall_back_to_structural_lines(self):
        rule = drpy.rule_of({"rule": SAMPLE})
        tree = drpy._parser(DETAIL_JS)
        lines = drpy._detail_lines(tree, rule["二级"], "https://demo.invalid/a.html")
        # 磁力组被跳过；帮助文本组没有链接，也不算线路
        self.assertEqual(list(lines),
                         ["播放地址（无插件 极速播放）", "播放地址四"])
        self.assertNotIn("磁力", json.dumps(lines, ensure_ascii=False))
        self.assertEqual(len(lines["播放地址（无插件 极速播放）"]), 2)
        self.assertEqual(lines["播放地址（无插件 极速播放）"][0]["title"], "第01集")
        self.assertTrue(lines["播放地址（无插件 极速播放）"][0]["url"]
                        .startswith("https://demo.invalid/e/play/"))

    def test_playlist_containers_become_separate_lines(self):
        rule = {"tabs": "h3:eq(0)", "lists": "#playlist1 ul li a"}
        tree = drpy._parser(DETAIL_MULTI)
        lines = drpy._detail_lines(tree, rule, "https://demo.invalid/a.html")
        self.assertEqual(len(lines), 2)
        self.assertEqual(sorted(len(v) for v in lines.values()), [1, 2])

    def test_plain_selector_single_line(self):
        rule = {"tabs": "h3:eq(0)", "lists": "#playlist1 ul li a"}
        html = ("<h3>视频列表</h3><div id='playlist1'><ul>"
                "<li><a href='/v/1.html'>第1集</a></li></ul></div>")
        lines = drpy._detail_lines(drpy._parser(html), rule,
                                  "https://demo.invalid/a.html")
        self.assertEqual(list(lines), ["视频列表"])


class PlayResolveTest(unittest.TestCase):
    def test_player_data_encrypt2(self):
        import base64
        import urllib.parse
        real = "https://cdn.invalid/hls/index.m3u8"
        enc = base64.b64encode(
            urllib.parse.quote(real, safe="").encode()).decode()
        html = ('<script>var player_data = {"url":"%s","encrypt":2};</script>'
                % enc)
        self.assertEqual(drpy._extract_media(html, "https://demo.invalid/p"),
                         real)

    def test_player_data_plain_url(self):
        html = ('<script>var player_aaaa={"url":"https:\\/\\/cdn.invalid\\/a.mp4",'
                '"encrypt":0}</script>')
        self.assertEqual(drpy._extract_media(html, "https://demo.invalid/p"),
                         "https://cdn.invalid/a.mp4")

    def test_direct_m3u8_in_page(self):
        html = ('<script>var player=new Aliplayer({source: '
                '"https://vip.invalid/2026/x/index.m3u8"});</script>')
        self.assertEqual(
            drpy._extract_media(html, "https://demo.invalid/p"),
            "https://vip.invalid/2026/x/index.m3u8")

    def test_protocol_relative_media(self):
        html = '<video src="//cdn.invalid/v.m3u8"></video>'
        self.assertEqual(drpy._extract_media(html, "https://demo.invalid/p"),
                         "https://cdn.invalid/v.m3u8")

    def test_iframe_only_returns_empty(self):
        html = ('<iframe src="https://vip.invalid/share/abc"></iframe>')
        self.assertEqual(drpy._extract_media(html, "https://demo.invalid/p"), "")

    def test_direct_url_short_circuits(self):
        import asyncio

        async def run():
            async with httpx.AsyncClient() as client:
                return await drpy.resolve(
                    client, {"id": "x", "kind": "drpy", "rule": SAMPLE},
                    "https://cdn.invalid/index.m3u8")
        self.assertEqual(asyncio.run(run()), "https://cdn.invalid/index.m3u8")


class SearchRedirectTest(unittest.IsolatedAsyncioTestCase):
    """POST 搜索遇到 http→https 的 301 时必须保留请求体。

    真实事故：httpx 的 follow_redirects 会按标准把 301 的 POST 降级成不带
    body 的 GET，6v 就回「没有搜索到相关的内容」，界面看起来像源坏了。
    """

    async def test_post_body_survives_scheme_redirect(self):
        calls = []

        def handler(request: httpx.Request):
            calls.append((str(request.url), request.method,
                          (request.content or b"").decode()))
            if request.url.path == "/e/search/index.php":
                # 6v 这类主题规则里写的是 index.php，实际入口是 so.php
                return httpx.Response(404)
            if request.url.scheme == "http":
                # http → https 的升级跳转：路径不变，POST 与请求体必须保留
                return httpx.Response(
                    301, headers={"location":
                                  "https://demo.invalid/e/search/so.php"})
            if request.url.path == "/e/search/so.php":
                return httpx.Response(
                    302, headers={"location":
                                  "https://demo.invalid/e/search/result/?searchid=1"})
            return httpx.Response(200, text=BILLI)

        src = {"id": "s", "name": "样例", "kind": "drpy", "api": "",
               "rule": SAMPLE.replace("host: 'https://demo.invalid'",
                                      "host: 'http://demo.invalid'")}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rows = await drpy.search_titles(client, src, "庆余年", 1)
        steps = [(c[0].split("/e/search/")[1].split("?")[0].split("/")[0], c[1])
                 for c in calls]
        self.assertEqual(steps[0], ("index.php", "POST"))
        self.assertEqual(steps[1], ("so.php", "POST"))
        # 换 scheme 之后的同一路径仍然是 POST，请求体没丢
        self.assertEqual(steps[2], ("so.php", "POST"))
        self.assertIn("keyboard=庆余年", calls[2][2])
        # 换到结果页才降级成 GET
        self.assertEqual(steps[3], ("result", "GET"))
        self.assertEqual(len(rows), 2)


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_cms_search_dispatches_to_drpy(self):
        src = {"id": "s", "name": "样例", "kind": "drpy", "api": "",
               "rule": SAMPLE}
        original = drpy.search_titles
        hit = {}

        async def fake(client, source, keyword, page=1):
            hit["keyword"] = keyword
            return []

        drpy.search_titles = fake
        try:
            async with httpx.AsyncClient() as client:
                await cms.search_source(client, src, "关键词", timeout=1)
        finally:
            drpy.search_titles = original
        self.assertEqual(hit.get("keyword"), "关键词")

    async def test_enabled_by_kind_only(self):
        # 路由里只剩 id/名称/api/kind（没有 rule），必须仍认成 drpy 源，
        # 否则翻页会走 CMS 的 ac=detail 分支拿到空页。
        self.assertTrue(drpy.enabled({"kind": "drpy"}))
        self.assertTrue(drpy.enabled({"kind": "drpy", "rule": "  "}))
        self.assertTrue(drpy.enabled({"kind": "drpy", "rule": "var rule={host:'x'}"}))
        self.assertFalse(drpy.enabled({"kind": "cms", "api": "http://x"}))
        self.assertFalse(drpy.enabled(None))

    def test_find_source_for_url_matches_rule_host(self):
        """CMS 源给的上游播放页要能按域名借同站 drpy 规则解析。

        bd/bd12 采集站会把 ``vod_play_url`` 填成 dbku.tv/vodplay/*.html，
        不是直链；只有本条是 drpy 源时现有逻辑才会解析，结果一直 502。
        """
        src = {"id": "dr_test", "name": "独播库", "kind": "drpy",
               "api": "https://www.dbku.tv", "enabled": True,
               "rule": SAMPLE}
        cfg = config.get()
        original = list(cfg.get("sources") or [])
        cfg["sources"] = original + [src]
        config.save(cfg)
        try:
            found = drpy.find_source_for_url(
                "https://www.dbku.tv/vodplay/150279-1-1.html")
            self.assertIsNotNone(found)
            self.assertEqual(found.get("id"), "dr_test")
            self.assertIsNone(drpy.find_source_for_url(
                "https://other.invalid/vodplay/1.html"))
        finally:
            cfg = config.get()
            cfg["sources"] = original
            config.save(cfg)

    def test_ensure_lines_retries_when_detail_empty(self):
        """详情页偶发空页要重试一次：不重试就是「搜索里点播放没反应」。"""
        import asyncio
        from unittest.mock import patch

        import config

        cfg = config.get()
        orig = cfg.get("sources")
        cfg["sources"] = [{"id": "dr_test", "name": "样例", "kind": "drpy",
                           "api": "https://demo.invalid", "rule": SAMPLE,
                           "enabled": True}]
        config.save(cfg)
        calls = []

        async def fake_detail(client, source, url):
            calls.append(url)
            if len(calls) == 1:
                return None                      # 第一次：源站空页
            return {"title": "谷雨", "cover": "", "desc": "", "content": "",
                    "lines": {"视频列表": [
                        {"title": "第1集",
                         "url": "https://demo.invalid/p-1.html",
                         "direct": False, "headers": {}},
                        {"title": "第2集",
                         "url": "https://demo.invalid/p-2.html",
                         "direct": False, "headers": {}}]}}

        try:
            store = cms.TitleStore()
            brief = {"guid": "a" * 32, "provider": "dr_test", "title": "谷雨",
                     "vod_id": "https://demo.invalid/v/1.html",
                     "lines": {}, "line_names": []}
            store.put_brief(brief)
            with patch.object(drpy, "detail", fake_detail):
                out = asyncio.run(drpy.ensure_lines(None, store, brief))
            self.assertEqual(len(calls), 2)
            self.assertEqual(list(out.get("lines") or {}), ["视频列表"])
            self.assertTrue(out.get("is_tv"))
        finally:
            cfg["sources"] = orig
            config.save(cfg)


if __name__ == "__main__":
    unittest.main()
