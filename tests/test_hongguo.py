import _bootstrap
import asyncio
import json
import unittest
from unittest.mock import patch

import catalog
import cms
import config
import hongguo

SOURCE = {"id": "hg_test", "name": "红果", "api": "http://hg.invalid:8787/",
          "kind": "hongguo", "token": "", "enabled": True}

HOME = {
    "source": "hongguo", "kind": "builtin",
    "classes": [{"id": "1", "name": "都市"}, {"id": "2", "name": "古装"}],
    "items": [{"id": "100", "title": "示例短剧", "cover": "http://img.invalid/a.jpg",
               "remarks": "全80集", "tags": ["都市"], "year": "2026",
               "area": "内地", "desc": "简介", "score": 8.6}],
    "page": 1, "hasMore": True,
}

CATEGORY = {
    "source": "hongguo", "tid": "1", "page": 1, "hasMore": True,
    "items": [{"id": "101", "title": "分类短剧", "cover": "", "remarks": "更新至10集"}],
}

SEARCH = {
    "source": "hongguo", "page": 1, "hasMore": False,
    "items": [{"id": "102", "title": "搜到的短剧", "cover": "http://img.invalid/b.jpg"}],
}

DETAIL = {
    "source": "hongguo", "id": "100", "title": "示例短剧",
    "cover": "http://img.invalid/c.jpg", "desc": "详细简介", "remarks": "全80集",
    "tags": ["都市"],
    "lines": [{"flag": "hg", "name": "红果（服务端解密）",
               "episodes": [{"id": "9001", "name": "第1集"},
                            {"id": "9002", "name": "第2集"}]}],
}

DETAIL_JS = {
    "source": "js-x", "id": "100", "title": "示例短剧",
    "lines": [{"flag": "js-x", "name": "线路 1",
               "episodes": [{"id": "ep1", "name": "第1集"}]}],
}

HEALTH = {"service": "hongguo-web", "ok": True, "version": "0.14.0",
          "active": "hongguo", "sources": 7}


class FakeResponse:
    def __init__(self, status: int, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeClient:
    """按路径后缀路由的假 httpx 客户端，并记录实际请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {})})
        for suffix, data in self.routes.items():
            if url.endswith(suffix):
                if isinstance(data, tuple):
                    status, payload = data
                    return FakeResponse(status, payload)
                return FakeResponse(200, data)
        return FakeResponse(404, {"error": "not found", "code": "NOT_FOUND"})


class HongguoAdapterTest(unittest.TestCase):

    def test_new_id_stable_and_address_bound(self):
        a = hongguo.new_id("http://192.168.1.5:8787")
        b = hongguo.new_id("http://192.168.1.5:8787")
        c = hongguo.new_id("http://192.168.1.6:8787")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("hg_"))

    def test_only_kind_hongguo_is_handled(self):
        self.assertTrue(hongguo.enabled(SOURCE))
        self.assertFalse(hongguo.enabled({"api": "https://a.invalid/api"}))
        self.assertFalse(hongguo.enabled(None))

    def test_stream_url_carries_token_and_quality(self):
        source = dict(SOURCE, token="abc def")
        url = hongguo.stream_url(source, "123", "auto")
        self.assertTrue(url.startswith("http://hg.invalid:8787/api/stream/123?q=auto"))
        self.assertIn("at=abc%20def", url)

    def test_titles_map_fields_and_mark_as_tv(self):
        rows = hongguo.titles(SOURCE, HOME["items"])
        self.assertEqual(len(rows), 1)
        item = rows[0]
        self.assertEqual((item.provider, item.vod_id), ("hg_test", "100"))
        self.assertEqual(item.title, "示例短剧")
        self.assertEqual(item.poster, "http://img.invalid/a.jpg")
        self.assertEqual(item.remarks, "全80集")
        self.assertEqual(item.score, "8.6")
        self.assertEqual(item.type_name, "短剧")
        # 短剧即便是单集也要按剧集展示，客户端才会走选集/续播
        self.assertTrue(cms.is_tv(item))
        # 列表阶段不带播放地址，必须靠详情补
        self.assertEqual(item.lines, {})

    def test_titles_skip_rows_without_id_or_title(self):
        rows = hongguo.titles(SOURCE, [{"id": "", "title": "x"},
                                       {"id": "1", "title": ""},
                                       {"id": "2", "title": "有"}])
        self.assertEqual([r.vod_id for r in rows], ["2"])

    def test_lines_of_native_uses_stream_endpoint(self):
        lines, native = hongguo.lines_of(SOURCE, DETAIL)
        self.assertTrue(native)
        self.assertEqual(list(lines), ["红果（服务端解密）"])
        eps = lines["红果（服务端解密）"]
        self.assertEqual([e.title for e in eps], ["第1集", "第2集"])
        self.assertEqual(eps[0].url,
                         "http://hg.invalid:8787/api/stream/9001?q=auto")
        self.assertTrue(eps[0].direct)

    def test_lines_of_js_source_uses_placeholder(self):
        lines, native = hongguo.lines_of(SOURCE, DETAIL_JS)
        self.assertFalse(native)
        eps = lines["线路 1"]
        self.assertTrue(eps[0].url.startswith("hgplay://"))
        self.assertFalse(eps[0].direct)

    def test_probe_rejects_non_hongguo_service(self):
        client = FakeClient({"/health": {"service": "something-else"}})
        result = asyncio.run(hongguo.probe(client, SOURCE))
        self.assertFalse(result["ok"])
        self.assertIn("不是", result["message"])

    def test_probe_accepts_hongguo_web(self):
        client = FakeClient({"/health": HEALTH})
        result = asyncio.run(hongguo.probe(client, SOURCE))
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"], "0.14.0")
        self.assertEqual(result["active"], "hongguo")

    def test_classes_reads_home_classes(self):
        client = FakeClient({"/api/home": HOME})
        rows = asyncio.run(hongguo.classes(client, SOURCE))
        self.assertEqual(rows, [{"id": "1", "name": "都市"},
                                {"id": "2", "name": "古装"}])

    def test_category_and_search_hit_their_endpoints(self):
        client = FakeClient({"/api/category": CATEGORY, "/api/search": SEARCH})
        page = asyncio.run(hongguo.page_titles(client, SOURCE, "1", 2))
        found = asyncio.run(hongguo.search_titles(client, SOURCE, "短剧"))
        self.assertEqual([t.vod_id for t in page], ["101"])
        self.assertEqual([t.vod_id for t in found], ["102"])
        self.assertEqual(client.calls[0]["params"],
                         {"tid": "1", "page": 2})
        self.assertEqual(client.calls[1]["params"], {"q": "短剧", "page": 1})

    def test_token_sent_as_both_header_and_query(self):
        client = FakeClient({"/api/search": SEARCH})
        source = dict(SOURCE, token="t0ken")
        asyncio.run(hongguo.search_titles(client, source, "x"))
        call = client.calls[0]
        self.assertEqual(call["headers"].get("Authorization"), "Bearer t0ken")
        self.assertEqual(call["params"].get("at"), "t0ken")

    def test_ensure_lines_fills_brief_and_registers_episodes(self):
        store = cms.TitleStore()
        brief = {"guid": "g" * 32, "provider": "hg_test", "vod_id": "100",
                 "title": "示例短剧", "lines": {}, "line_names": [],
                 "episodes": 0, "is_tv": False, "poster": "", "overview": ""}
        store._items[brief["guid"]] = brief
        client = FakeClient({"/api/detail": DETAIL})
        with patch.object(hongguo, "find_source", return_value=SOURCE):
            updated = asyncio.run(hongguo.ensure_lines(client, store, brief))
        self.assertEqual(updated["episodes"], 2)
        self.assertTrue(updated["is_tv"])
        self.assertEqual(updated["primary_ext"], "mp4")
        self.assertEqual(updated["line_sources"], {"红果（服务端解密）": "hg_test"})
        # 已写回 store，且分集映射可用（播放链靠它反查）
        self.assertEqual(store.get(brief["guid"])["episodes"], 2)
        self.assertIsNotNone(store.resolve_episode(
            __import__("fake").episode_guid(brief["guid"], 1)))

    def test_ensure_lines_keeps_brief_when_detail_empty(self):
        store = cms.TitleStore()
        brief = {"guid": "h" * 32, "provider": "hg_test", "vod_id": "100",
                 "title": "示例短剧", "lines": {}}
        client = FakeClient({})  # 404
        with patch.object(hongguo, "find_source", return_value=SOURCE):
            out = asyncio.run(hongguo.ensure_lines(client, store, brief))
        self.assertIs(out, brief)
        self.assertEqual(out["lines"], {})

    def test_ensure_lines_is_noop_without_lines_gap(self):
        store = cms.TitleStore()
        brief = {"guid": "i" * 32, "provider": "hg_test", "vod_id": "100",
                 "lines": {"线路": [{"url": "http://x/1.m3u8"}]}}
        client = FakeClient({"/api/detail": DETAIL})
        with patch.object(hongguo, "find_source", return_value=SOURCE):
            out = asyncio.run(hongguo.ensure_lines(client, store, brief))
        self.assertIs(out, brief)
        self.assertEqual(client.calls, [])


class HongguoIntegrationTest(unittest.TestCase):
    """接入点：搜索、分类表、直链判定。"""

    def setUp(self):
        cfg = config.get()
        self._orig = {k: cfg.get(k) for k in ("sources", "acl_enabled")}
        cfg["sources"] = [dict(SOURCE)]
        cfg["acl_enabled"] = False
        config.save(cfg)

    def tearDown(self):
        cfg = config.get()
        cfg.update(self._orig)
        config.save(cfg)

    def test_search_source_routes_to_hongguo(self):
        client = FakeClient({"/api/search": SEARCH})
        found = asyncio.run(cms.search_source(client, SOURCE, "短剧", timeout=3))
        self.assertEqual([t.provider for t in found], ["hg_test"])
        self.assertEqual([t.title for t in found], ["搜到的短剧"])

    def test_client_get_classes_converts_to_cms_shape(self):
        client = FakeClient({"/api/home": HOME})
        payload = asyncio.run(catalog.client_get_classes(client, SOURCE))
        self.assertEqual(payload["code"], 1)
        rows = catalog.class_rows(payload)
        self.assertEqual([r["id"] for r in rows], ["1", "2"])
        self.assertEqual([r["name"] for r in rows], ["都市", "古装"])

    def test_hg_brief_helper_fills_lines_for_playback(self):
        """详情/起播入口必须能把「只有标题」的红果条目补成可播放。"""
        import app as appmod
        from types import SimpleNamespace

        store = cms.TitleStore()
        brief = {"guid": "j" * 32, "provider": "hg_test", "vod_id": "100",
                 "title": "示例短剧", "lines": {}, "line_names": [],
                 "episodes": 0, "is_tv": True, "poster": "", "overview": ""}
        store.put_brief(brief)
        client = FakeClient({"/api/detail": DETAIL})
        request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(src=client, store=store)))
        with patch.object(hongguo, "find_source", return_value=SOURCE):
            out = asyncio.run(appmod._hg_brief(request, brief))
        self.assertEqual(out["episodes"], 2)
        url, _ = appmod._pick(out, 0, 1)
        self.assertIn("/api/stream/9002", url)

    def test_pending_brief_still_reaches_detail_when_acl_on(self):
        """ACL 开启时，没有分集的条目不能被判成失效。

        否则详情请求会落回官方透传，客户端显示「该影片已被移除」——
        这是「分类能进去、点进去就报错」的直接原因。
        """
        import acl
        import app as appmod

        cfg = config.get()
        orig = {k: cfg.get(k) for k in
                ("acl_enabled", "acl_grants", "acl_default_visible",
                 "acl_default_online")}
        cfg.update({"acl_enabled": True, "acl_grants": {},
                    "acl_default_visible": True, "acl_default_online": True})
        config.save(cfg)
        acl.invalidate()
        token = acl.push(acl.Identity(uid="u1", name="u1", via="session"))
        try:
            store = cms.TitleStore()
            brief = {"guid": "k" * 32, "provider": "hg_test", "vod_id": "100",
                     "title": "示例短剧", "lines": {}, "line_names": [],
                     "episodes": 0, "is_tv": True, "poster": "", "overview": ""}
            store.put_brief(brief)
            self.assertIsNotNone(appmod._brief(store, "k" * 32))
        finally:
            acl.reset(token)
            cfg.update(orig)
            config.save(cfg)
            acl.invalidate()

    def test_catalog_page_reads_token_from_live_source(self):
        """路由只存 id/名称，翻页必须取回完整源配置，否则 TOKEN 部署会 401。"""
        cfg = config.get()
        cfg["sources"] = [dict(SOURCE, token="t0ken")]
        config.save(cfg)
        seen = {}

        async def fake_page(client, source, tid, page=1):
            seen["token"] = source.get("token")
            return []

        route = {"api": "http://hg.invalid:8787/", "type_id": "1",
                 "source_id": "hg_test", "source_name": "红果",
                 "kind": "hongguo", "total": 0}
        with patch.object(hongguo, "page_titles", new=fake_page):
            asyncio.run(catalog.catalog._page(None, route, 7))
        self.assertEqual(seen.get("token"), "t0ken")

    def test_item_detail_returns_playable_lines_for_pending_card(self):
        """分类卡片点进详情：ACL 开启也要补出分集，不能落回官方透传。"""
        import acl
        import app as appmod
        from types import SimpleNamespace

        cfg = config.get()
        orig = {k: cfg.get(k) for k in
                ("sources", "acl_enabled", "acl_grants",
                 "acl_default_visible", "acl_default_online")}
        cfg.update({"sources": [dict(SOURCE)], "acl_enabled": True,
                    "acl_grants": {}, "acl_default_visible": True,
                    "acl_default_online": True})
        config.save(cfg)
        acl.invalidate()
        try:
            store = cms.TitleStore()
            guid = store.remember(hongguo.titles(SOURCE, HOME["items"]))[0]["guid"]
            client = FakeClient({"/api/detail": DETAIL})
            request = SimpleNamespace(
                headers={}, query_params={},
                app=SimpleNamespace(state=SimpleNamespace(
                    src=client, store=store, poster=None)))
            resp = asyncio.run(appmod.item_detail(guid, request))
            data = json.loads(bytes(resp.body))["data"]
            self.assertEqual(data["title"], "示例短剧")
            self.assertEqual(data["can_play"], 1)
            self.assertEqual(data["number_of_episodes"], 2)
        finally:
            cfg.update(orig)
            config.save(cfg)
            acl.invalidate()

    def test_merge_keeps_pending_source_as_separate_card(self):
        """红果卡没有播放地址，不能被同名 CMS 卡合并吃掉。"""
        ready = cms.OnlineTitle(
            provider="s_a", vod_id="1", title="同名剧",
            lines={"线路A": [cms.Episode("第1集", "http://a/1.m3u8")]})
        pending = cms.OnlineTitle(provider="hg_x", vod_id="2",
                                  title="同名剧", type_name="短剧")
        merged = cms.merge_titles([ready, pending])
        self.assertEqual(len(merged), 2)
        self.assertEqual({m.provider for m in merged}, {"s_a", "hg_x"})

    def test_app_treats_stream_url_as_direct_media(self):
        import app as appmod
        url = hongguo.stream_url(SOURCE, "9001")
        self.assertTrue(appmod._is_direct_media(url))
        self.assertFalse(appmod._is_direct_media("http://x/a.m3u8"))
        self.assertFalse(appmod._is_direct_media("http://x/watch/1"))


if __name__ == "__main__":
    unittest.main()
