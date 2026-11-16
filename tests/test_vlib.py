import _bootstrap
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import acl
import app as appmod
import catalog
import cms
import charts
import config
import fake
import vlib


def chart_item(cat: str, index: int) -> dict:
    douban_id = f"{cat}-{index}"
    return {
        "id": douban_id, "guid": charts.chart_guid(cat, douban_id), "cat": cat,
        "title": f"榜单{cat}{index}", "rating": 8.4,
        "cover": f"https://img.invalid/{douban_id}.jpg",
        "year": "2025", "subtitle": "2025 / 中国", "desc": "简介",
        "tags": ["剧情"], "origin": "douban-tag",
    }


def seed_charts(counts: dict[str, int] | None = None) -> dict:
    counts = counts or {"movie": 4, "tv": 3, "anime": 2, "variety": 1}
    data = {
        "updated_at": int(time.time()), "provider_ok": 1, "provider_total": 1,
        "charts": {cat: [chart_item(cat, i) for i in range(n)]
                   for cat, n in counts.items()},
    }
    charts.chart_cache._apply(data)
    return data


def make_title(vod_id="vlib-1", title="虚拟库测试", type_name="电影",
               episodes=1) -> cms.OnlineTitle:
    return cms.OnlineTitle(
        provider="s_1", vod_id=vod_id, title=title, type_name=type_name,
        year="2025",
        lines={"m3u8": [cms.Episode(f"第{i + 1}集",
                                    f"https://a.invalid/{vod_id}-{i}.m3u8")
                        for i in range(episodes)]})


class VlibTest(unittest.TestCase):
    def setUp(self):
        cfg = config.get()
        self._orig_sources = list(cfg.get("sources") or [])
        cfg.update({"vlib_enabled": True, "vlib_charts": True,
                    "vlib_catalog": True, "vlib_refresh_hours": 24,
                    "sources": [{"id": "s_1", "name": "源",
                                 "api": "https://a.invalid/api",
                                 "enabled": True}],
                    "enabled_limit": 10})
        config.save(cfg)
        seed_charts()
        catalog.catalog._routes = {}
        catalog.catalog._updated_at = time.time()
        catalog.catalog._pages.clear()
        vlib._BINDS.clear()
        vlib._FAILED.clear()

    def tearDown(self):
        charts.chart_cache.reset()
        catalog.catalog.reset()
        vlib._BINDS.clear()
        vlib._FAILED.clear()
        cfg = config.get()
        cfg["sources"] = self._orig_sources
        config.save(cfg)

    # ---------------------------------------------------------- 基础
    def test_guid_namespaces_are_stable_and_distinct(self):
        self.assertEqual(len(set(vlib.lib_guids())), 8)
        self.assertEqual(
            vlib.CHART_LIB_GUIDS["movie"],
            __import__("hashlib").md5(
                b"fnmedia-vlib:chart:movie").hexdigest())
        self.assertTrue(vlib.is_virtual_lib(vlib.CHART_LIB_GUIDS["tv"]))
        self.assertTrue(vlib.is_virtual_lib(vlib.CATALOG_LIB_GUIDS["anime"]))
        self.assertFalse(vlib.is_virtual_lib("local-guid"))

    def test_bucket_of_maps_cms_categories(self):
        self.assertEqual(catalog.bucket_of("动作片"), "movie")
        self.assertEqual(catalog.bucket_of("国产剧"), "tv")
        self.assertEqual(catalog.bucket_of("日本动漫"), "anime")
        self.assertEqual(catalog.bucket_of("大陆综艺"), "variety")
        self.assertIsNone(catalog.bucket_of("电影解说"))
        self.assertIsNone(catalog.bucket_of("伦理片"))
        self.assertIsNone(catalog.bucket_of("短剧"))

    # ---------------------------------------------------------- 媒体库列表
    def test_mediadb_list_injects_libraries_and_keeps_local(self):
        upstream = {"code": 0, "msg": "",
                    "data": [{"guid": "local-1", "title": "本地影片"}]}
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse(upstream))):
            with TestClient(appmod.app) as client:
                data = client.get("/v/api/v1/mediadb/list").json()["data"]
        guids = [row["guid"] for row in data]
        self.assertEqual(guids[-1], "local-1")
        self.assertIn(vlib.CHART_LIB_GUIDS["movie"], guids)
        self.assertIn(vlib.CHART_LIB_GUIDS["tv"], guids)
        self.assertEqual(guids.count(vlib.CHART_LIB_GUIDS["movie"]), 1)
        movie = next(row for row in data
                     if row["guid"] == vlib.CHART_LIB_GUIDS["movie"])
        self.assertEqual(movie["category"], "Movie")
        self.assertTrue(movie["poster"].startswith("/online/"))

    def test_mediadb_list_strips_duplicated_virtual_rows(self):
        upstream = {"code": 0, "msg": "", "data": [
            {"guid": vlib.CHART_LIB_GUIDS["movie"], "title": "旧虚拟库"},
            {"guid": "local-1", "title": "本地影片"},
        ]}
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse(upstream))):
            with TestClient(appmod.app) as client:
                data = client.get("/v/api/v1/mediadb/list").json()["data"]
        self.assertEqual([row["guid"] for row in data].count(
            vlib.CHART_LIB_GUIDS["movie"]), 1)

    def test_mediadb_list_disabled_is_passthrough(self):
        cfg = config.get()
        cfg["vlib_enabled"] = False
        config.save(cfg)
        upstream = {"code": 0, "msg": "", "data": [{"guid": "local-1"}]}
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse(upstream))):
            with TestClient(appmod.app) as client:
                data = client.get("/v/api/v1/mediadb/list").json()["data"]
        self.assertEqual(data, [{"guid": "local-1"}])

    def test_mdb_detail_and_sum(self):
        with patch("app._passthrough", AsyncMock(return_value=JSONResponse(
                {"code": 0, "msg": "", "data": {"total": 10}}))):
            with TestClient(appmod.app) as client:
                detail = client.get(
                    f"/v/api/v1/mdb/{vlib.CHART_LIB_GUIDS['tv']}").json()["data"]
                self.assertEqual(detail["category"], "TV")
                self.assertIn("热门剧集", detail["name"])
                self.assertTrue(detail["refresh_disabled"])
                summary = client.get("/v/api/v1/mediadb/sum").json()["data"]
        self.assertEqual(summary[vlib.CHART_LIB_GUIDS["movie"]], 4)
        self.assertEqual(summary[vlib.CHART_LIB_GUIDS["tv"]], 3)
        self.assertEqual(summary["total"], 10 + 10)

    def test_library_root_item_is_mediadb(self):
        """进入媒体库先请求 item/<库guid>，必须回 MediaDB 根节点而不是透传。"""
        upstream = JSONResponse({"code": -6, "msg": "此媒体库不存在",
                                 "data": None})
        with patch("app._passthrough", AsyncMock(return_value=upstream)):
            with TestClient(appmod.app) as client:
                for cat, count in (("movie", 4), ("tv", 3)):
                    payload = client.get(
                        f"/v/api/v1/item/{vlib.CHART_LIB_GUIDS[cat]}").json()
                    self.assertEqual(payload["code"], 0)
                    row = payload["data"]
                    self.assertEqual(row["type"], "MediaDB")
                    self.assertEqual(row["number_of_episodes"], count)
                    self.assertEqual(row["play_item_guid"], "")
                    self.assertEqual(row["ancestor_guid"], "")
                    self.assertIn("热门", row["title"])

    def test_disabled_library_root_falls_through(self):
        cfg = config.get()
        cfg.update({"vlib_charts": False})
        config.save(cfg)
        upstream = JSONResponse({"code": -6, "msg": "此媒体库不存在",
                                 "data": None})
        with patch("app._passthrough", AsyncMock(return_value=upstream)):
            with TestClient(appmod.app) as client:
                payload = client.get(
                    f"/v/api/v1/item/{vlib.CHART_LIB_GUIDS['movie']}").json()
        self.assertEqual(payload["code"], -6)

    # ---------------------------------------------------------- 榜单分页
    def test_chart_item_list_pages(self):
        with TestClient(appmod.app) as client:
            resp = client.post("/v/api/v1/item/list", json={
                "ancestor_guid": vlib.CHART_LIB_GUIDS["movie"],
                "page": 2, "page_size": 3}).json()["data"]
        self.assertEqual(resp["total"], 4)
        self.assertEqual(len(resp["list"]), 1)
        self.assertEqual(resp["mdb_name"], "🎬热门电影")
        self.assertEqual(resp["list"][0]["type"], "Movie")
        self.assertTrue(resp["list"][0]["guid"])

    def test_chart_item_list_offset_window(self):
        with TestClient(appmod.app) as client:
            resp = client.post("/v/api/v1/item/list", json={
                "ancestor_guid": vlib.CHART_LIB_GUIDS["movie"],
                "offset": 1, "page_size": 2}).json()["data"]
        self.assertEqual(len(resp["list"]), 2)
        self.assertIn("榜单movie1", resp["list"][0]["title"])

    # ---------------------------------------------------------- 绑定
    def test_ensure_bound_maps_chart_guid_to_playable_brief(self):
        meta = charts.chart_cache.get(
            charts.chart_guid("movie", "movie-0"))
        store = cms.TitleStore()
        with patch("cms.search_all", new=AsyncMock(
                return_value=[make_title(title=meta["title"])])):
            brief = asyncio.run(vlib.ensure_bound(store, None, meta["guid"]))
        self.assertIsNotNone(brief)
        self.assertEqual(brief["guid"], meta["guid"])
        self.assertEqual(store.get(meta["guid"])["title"], meta["title"])
        resolved = store.resolve_media(fake.media_guid(meta["guid"]))
        self.assertEqual(resolved, (meta["guid"], 0))

    def test_bind_prefers_exact_title_and_year(self):
        meta = charts.chart_cache.get(charts.chart_guid("movie", "movie-0"))
        wrong = make_title(vod_id="w1", title=meta["title"] + "外传")
        right = make_title(vod_id="w2", title=meta["title"])
        picked = vlib._pick_match(meta, [wrong, right])
        self.assertIs(picked, right)
        self.assertIsNone(vlib._pick_match(meta, [make_title(title="毫无关系")]))

    def test_chart_detail_unbound_reports_no_source(self):
        meta = charts.chart_cache.get(charts.chart_guid("movie", "movie-1"))
        with patch("cms.search_all", new=AsyncMock(return_value=[])):
            with TestClient(appmod.app) as client:
                data = client.get(f"/v/api/v1/item/{meta['guid']}").json()["data"]
        self.assertEqual(data["can_play"], 0)
        self.assertIn("暂无", data["overview"])

    def test_chart_detail_bound_is_playable(self):
        meta = charts.chart_cache.get(charts.chart_guid("movie", "movie-2"))
        with patch("cms.search_all", new=AsyncMock(
                return_value=[make_title(title=meta["title"])])):
            with TestClient(appmod.app) as client:
                data = client.get(f"/v/api/v1/item/{meta['guid']}").json()["data"]
                info = client.post("/v/api/v1/play/info",
                                   json={"item_guid": meta["guid"]}).json()["data"]
        self.assertEqual(data["can_play"], 1)
        self.assertEqual(data["title"], meta["title"])
        self.assertEqual(info["ts"], 0)
        self.assertTrue(info["media_guid"])

    def test_chart_of_media_reverse_lookup(self):
        meta = charts.chart_cache.get(charts.chart_guid("tv", "tv-0"))
        self.assertEqual(vlib.chart_of_media(fake.media_guid(meta["guid"])),
                         meta["guid"])
        self.assertIsNone(vlib.chart_of_media("unknown-media"))

    def test_play_entries_accept_any_guid_kind(self):
        """播放四入口（play/info、play/play、stream）必须接受 item/media/video guid。"""
        meta = charts.chart_cache.get(charts.chart_guid("movie", "movie-3"))
        with patch("cms.search_all", new=AsyncMock(
                return_value=[make_title(title=meta["title"])])):
            with TestClient(appmod.app) as client:
                info = client.post("/v/api/v1/play/info",
                                   json={"item_guid": meta["guid"]}).json()["data"]
                mg = info["media_guid"]
                self.assertTrue(mg)
                # 只带 media_guid 调 play/info（部分客户端不传 item_guid）
                by_media = client.post("/v/api/v1/play/info",
                                       json={"media_guid": mg}).json()["data"]
                self.assertEqual(by_media["guid"], meta["guid"])
                # 只带 item_guid 调 play/play
                played = client.post("/v/api/v1/play/play",
                                     json={"item_guid": meta["guid"]}).json()["data"]
                self.assertIn("/preset.m3u8", played["play_link"])
                self.assertEqual(played["media_guid"], mg)
                # 直接带 video_guid 取流
                stream = client.post("/v/api/v1/stream", json={
                    "video_guid": by_media["video_guid"]}).json()["data"]
                self.assertTrue(stream["file_stream"]["guid"])
                self.assertEqual(client.get(played["play_link"]).status_code, 200)

    # ---------------------------------------------------------- 海量分类库
    def test_catalog_window_slices_across_pages(self):
        titles = [make_title(vod_id=f"c{i}", title=f"片{i}") for i in range(60)]
        pages = {1: titles[0:20], 2: titles[20:40], 3: titles[40:60]}
        catalog.catalog._routes = {"movie": {
            "source_id": "s_1", "source_name": "源", "api": "https://a.invalid/api",
            "type_id": "1", "type_name": "动作片", "pages": 3, "total": 60,
            "posters": ["https://img.invalid/p.jpg"]}}
        catalog.catalog._updated_at = time.time()

        async def fake_page(client, route, page):
            return pages.get(page, [])

        catalog.catalog._page = fake_page
        got, total = asyncio.run(
            catalog.catalog.window(None, "movie", 25, 10))
        self.assertEqual(total, 60)
        self.assertEqual(len(got), 10)
        self.assertEqual(got[0].title, "片25")
        self.assertEqual(got[-1].title, "片34")

    def test_catalog_item_list_returns_cards(self):
        titles = [make_title(vod_id=f"c{i}", title=f"片{i}") for i in range(5)]
        catalog.catalog._routes = {"movie": {
            "source_id": "s_1", "source_name": "源", "api": "https://a.invalid/api",
            "type_id": "1", "type_name": "动作片", "pages": 1, "total": 5,
            "posters": []}}
        catalog.catalog._updated_at = time.time()

        async def fake_page(client, route, page):
            return titles if page == 1 else []

        catalog.catalog._page = fake_page
        with TestClient(appmod.app) as client:
            data = client.post("/v/api/v1/item/list", json={
                "ancestor_guid": vlib.CATALOG_LIB_GUIDS["movie"],
                "page": 1, "page_size": 22}).json()["data"]
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["list"]), 5)
        card = data["list"][0]
        self.assertEqual(card["ancestor_guid"], vlib.CATALOG_LIB_GUIDS["movie"])
        self.assertEqual(card["ancestor_category"], "Movie")
        self.assertNotIn("【在线】", card["title"])

    def test_catalog_route_refresh_groups_categories(self):
        sources = [{"id": "s_1", "name": "源", "api": "https://a.invalid/api"}]

        async def fake_classes(client, source):
            return {"code": 1, "class": [
                {"type_id": 1, "type_name": "动作片"},
                {"type_id": 2, "type_name": "国产剧"},
                {"type_id": 3, "type_name": "日本动漫"},
                {"type_id": 4, "type_name": "大陆综艺"},
                {"type_id": 5, "type_name": "电影解说"},
            ]}

        async def fake_probe(client, source, row):
            return {"source_id": "s_1", "source_name": "源",
                    "api": source["api"], "type_id": str(row["type_id"]),
                    "type_name": row["type_name"], "pages": 10, "total": 200,
                    "posters": []}

        with patch("catalog.client_get_classes", new=fake_classes), \
                patch.object(catalog.catalog, "_probe", new=fake_probe):
            status = asyncio.run(
                catalog.catalog.refresh_routes(None, sources))
        self.assertEqual(set(status["routes"]) & {"movie", "tv", "anime",
                                                  "variety"},
                         {"movie", "tv", "anime", "variety"})
        self.assertEqual(catalog.catalog.route("movie")["type_id"], "1")


class VlibSourceMappingTest(unittest.TestCase):
    """片源映射：只有选中的源进虚拟库，且按账号授权自动过滤。"""

    def setUp(self):
        cfg = config.get()
        self._orig = {k: cfg.get(k) for k in (
            "sources", "vlib_sources", "vlib_categories", "vlib_class_ids",
            "vlib_split", "vlib_enabled", "vlib_charts", "vlib_catalog",
            "enabled_limit", "acl_enabled", "acl_grants",
            "acl_default_visible", "acl_default_online")}
        cfg.update({
            "sources": [
                {"id": "s_1", "name": "源一",
                 "api": "https://a.invalid/api", "enabled": True},
                {"id": "s_2", "name": "源二",
                 "api": "https://b.invalid/api", "enabled": True},
            ],
            "vlib_sources": ["s_1"],
            "vlib_categories": ["movie", "tv"],
            "vlib_class_ids": {},
            "vlib_split": "source_category",
            "vlib_enabled": True, "vlib_charts": False, "vlib_catalog": True,
            "enabled_limit": 10, "acl_enabled": False, "acl_grants": {},
            "acl_default_visible": True, "acl_default_online": True,
        })
        config.save(cfg)
        catalog.catalog._routes = {}
        catalog.catalog._classes = {
            "s_1": [
                {"id": "1", "name": "动作片", "junk": False, "mdb": "Movie"},
                {"id": "2", "name": "国产剧", "junk": False, "mdb": "TV"},
                {"id": "9", "name": "电影解说", "junk": True, "mdb": "Mix"},
            ],
            "s_2": [
                {"id": "3", "name": "喜剧片", "junk": False, "mdb": "Movie"},
            ],
        }
        catalog.catalog._updated_at = time.time()
        catalog.catalog._pages.clear()
        for sid, ids in (("s_1", ("1", "2")), ("s_2", ("3",))):
            for tid in ids:
                catalog.catalog._routes[f"{sid}:{tid}"] = {
                    "source_id": sid, "source_name": sid,
                    "api": "https://a.invalid/api", "type_id": tid,
                    "type_name": "动作片", "mdb": "Movie",
                    "pages": 1, "total": 8, "posters": []}
        vlib._BINDS.clear()
        vlib._FAILED.clear()
        vlib._INFLIGHT.clear()

    def tearDown(self):
        catalog.catalog.reset()
        charts.chart_cache.reset()
        vlib._BINDS.clear()
        vlib._FAILED.clear()
        cfg = config.get()
        cfg.update(self._orig)
        config.save(cfg)

    def test_only_mapped_source_creates_libraries(self):
        libs = vlib.libraries()
        self.assertEqual(len(libs), 2)
        self.assertTrue(all("源一" in row["title"] for row in libs))
        self.assertEqual({vlib.lib_key(row["guid"]) for row in libs},
                         {"src:s_1:1", "src:s_1:2"})

    def test_guid_and_library_keys_are_recoverable(self):
        guid = vlib.source_lib_guid("s_1", "1")
        self.assertEqual(vlib.source_lib_of(guid), ("s_1", "1"))
        self.assertTrue(vlib.is_virtual_lib(guid))
        self.assertIn(guid, vlib.lib_guids())
        self.assertIn("src:s_1:1",
                      [row["key"] for row in vlib.library_keys()])

    def test_account_source_grant_filters_libraries(self):
        cfg = config.get()
        cfg.update({"acl_enabled": True,
                    "acl_grants": {"u1": {"sources": ["s_2"]}}})
        config.save(cfg)
        token = acl.push(acl.Identity(uid="u1", name="u1", via="header"))
        try:
            self.assertEqual(vlib.libraries(), [])
        finally:
            acl.reset(token)

    def test_chart_binding_uses_only_mapped_sources(self):
        seed_charts()
        meta = charts.chart_cache.get(charts.chart_guid("movie", "movie-0"))
        seen = {}

        async def fake_search(client, sources, word, **kwargs):
            seen["sources"] = sources
            return []

        with patch("cms.search_all", new=fake_search):
            asyncio.run(vlib.ensure_bound(cms.TitleStore(), None,
                                          meta["guid"]))
        self.assertEqual([s["id"] for s in seen["sources"]], ["s_1"])

    def test_empty_mapping_keeps_legacy_behavior(self):
        cfg = config.get()
        cfg["vlib_sources"] = []
        config.save(cfg)
        self.assertFalse(vlib.split_by_source())
        self.assertEqual(len(set(vlib.lib_guids())), 8)
        self.assertEqual(vlib.mapped_sources(), [])

    def test_config_drops_unknown_source_ids(self):
        cfg = config.get()
        cfg["vlib_sources"] = ["s_1", "ghost"]
        config.save(cfg)
        self.assertEqual(config.get()["vlib_sources"], ["s_1"])

    def test_mapped_library_detail_root_and_item_list(self):
        guid = vlib.source_lib_guid("s_1", "1")
        titles = [make_title(vod_id="m1", title="映射片")]

        async def fake_page(client, route, page):
            return titles if page == 1 else []

        with patch.object(catalog.catalog, "_page", new=fake_page):
            with TestClient(appmod.app) as client:
                detail = client.get(f"/v/api/v1/mdb/{guid}").json()["data"]
                root = client.get(f"/v/api/v1/item/{guid}").json()["data"]
                listing = client.post("/v/api/v1/item/list", json={
                    "ancestor_guid": guid, "page": 1,
                    "page_size": 22}).json()["data"]
        self.assertIn("源一", detail["name"])
        self.assertEqual(root["type"], "MediaDB")
        self.assertEqual(listing["total"], 8)
        self.assertEqual(listing["list"][0]["ancestor_guid"], guid)
        self.assertIn("源一", listing["list"][0]["ancestor_name"])

    def test_mediadb_list_injects_only_mapped_libraries(self):
        with patch("app._passthrough", AsyncMock(return_value=JSONResponse(
                {"code": 0, "msg": "", "data": [{"guid": "local-1"}]}))):
            with TestClient(appmod.app) as client:
                rows = client.get("/v/api/v1/mediadb/list").json()["data"]
        virtual = [r for r in rows if r["guid"] != "local-1"]
        self.assertEqual(len(virtual), 2)
        self.assertTrue(all("源一" in r["title"] for r in virtual))

    def test_split_route_refresh_keys_by_source(self):
        catalog.catalog._routes["s_1:movie"] = {
            "type_id": "1", "source_id": "s_1", "total": 5}
        self.assertTrue(catalog.catalog.legacy_routes())
        sources = [{"id": "s_1", "name": "源一",
                    "api": "https://a.invalid/api"},
                   {"id": "s_2", "name": "源二",
                    "api": "https://b.invalid/api"}]

        async def fake_classes(client, source):
            return {"code": 1, "class": [
                {"type_id": 1, "type_name": "动作片"},
                {"type_id": 2, "type_name": "国产剧"},
                {"type_id": 3, "type_name": "日本动漫"},
                {"type_id": 4, "type_name": "大陆综艺"},
            ]}

        async def fake_probe(client, source, row):
            return {"source_id": source["id"], "source_name": source["name"],
                    "api": source["api"], "type_id": str(row["type_id"]),
                    "type_name": row["type_name"], "pages": 5, "total": 100,
                    "posters": []}

        with patch("catalog.client_get_classes", new=fake_classes), \
                patch.object(catalog.catalog, "_probe", new=fake_probe):
            status = asyncio.run(catalog.catalog.refresh_routes(
                None, sources, split=True))
        self.assertIn("s_1:1", status["routes"])
        self.assertIn("s_2:4", status["routes"])
        self.assertNotIn("movie", status["routes"])
        self.assertNotIn("s_1:movie", status["routes"])
        self.assertFalse(catalog.catalog.legacy_routes())
        self.assertIn("s_1", status["by_source"])
        self.assertEqual([r["id"] for r in catalog.catalog.classes("s_1")],
                         ["1", "2", "3", "4"])

    def test_stale_honours_refresh_interval(self):
        """stale 要能用管理页设的刷新间隔，而不是写死 6 小时。"""
        catalog.catalog._updated_at = time.time() - 3600
        self.assertTrue(catalog.catalog.stale(1800))
        self.assertFalse(catalog.catalog.stale(7200))

    def test_refresh_without_new_classes_marks_checked(self):
        """路由齐全、本轮无新分类时不再重复探源，但 updated_at 要刷新。

        否则 stale 永远为真，后台每 10 分钟白拉一遍所有源的分类表。
        """
        sources = [{"id": "s_1", "name": "源一", "api": "https://a.invalid/api"}]

        async def fake_classes(client, source):
            return {"code": 1, "class": [
                {"type_id": 1, "type_name": "动作片"},
                {"type_id": 2, "type_name": "国产剧"},
            ]}

        calls: list[str] = []

        async def fake_probe(client, source, row):
            calls.append(str(row.get("id")))
            return {"source_id": source["id"], "source_name": source["name"],
                    "api": source["api"], "type_id": str(row.get("id")),
                    "type_name": str(row.get("name")), "pages": 2, "total": 40,
                    "posters": []}

        with patch("catalog.client_get_classes", new=fake_classes), \
                patch.object(catalog.catalog, "_probe", new=fake_probe):
            asyncio.run(catalog.catalog.refresh_routes(
                None, sources, split=True, force=True))
            self.assertEqual(sorted(calls), ["1", "2"])
            catalog.catalog._updated_at = 0.0
            asyncio.run(catalog.catalog.refresh_routes(None, sources, split=True))
        self.assertEqual(len(calls), 2)  # 第二轮没再探
        self.assertGreater(catalog.catalog._updated_at, 0)
        self.assertIn("s_1:1", catalog.catalog._routes)

    def test_missing_split_routes_detects_pure_category_routes(self):
        """纯 <cat> 路由在拆库模式下必须判为「缺路由」并触发重建。

        以前判断只看 ``legacy_routes()``（<sid>:movie 键），升级后路由表整个
        是 movie/tv/anime/variety 时两者都不成立，客户端就永久只剩榜单四库。
        """
        sources = [{"id": "s_1", "name": "源一", "api": "https://a.invalid/api"}]
        catalog.catalog._routes = {
            "movie": {"type_id": "1", "source_id": "s_1", "total": 9},
            "tv": {"type_id": "2", "source_id": "s_1", "total": 9},
        }
        self.assertFalse(catalog.catalog.legacy_routes())
        self.assertTrue(catalog.catalog.missing_split_routes(sources))
        catalog.catalog._routes["s_1:5"] = {
            "type_id": "5", "source_id": "s_1", "total": 9}
        self.assertFalse(catalog.catalog.missing_split_routes(sources))
        # <sid>:四大类 这种旧键不能算「已建路由」
        catalog.catalog._routes = {
            "s_1:movie": {"type_id": "movie", "source_id": "s_1", "total": 9}}
        self.assertTrue(catalog.catalog.missing_split_routes(sources))
        self.assertFalse(catalog.catalog.missing_split_routes([]))

    def test_split_rebuild_writes_routes_before_all_probes_done(self):
        """探测中途就要有路由落盘，否则源站慢时客户端只看到空库。"""
        sources = [{"id": "s_1", "name": "源一", "api": "https://a.invalid/api"}]

        async def fake_classes(client, source):
            return {"code": 1, "class": [
                {"type_id": i, "type_name": f"类{i}"} for i in range(1, 13)]}

        seen: list[int] = []

        async def fake_probe(client, source, row):
            await asyncio.sleep(0.01)
            seen.append(len(catalog.catalog._routes))
            return {"source_id": source["id"], "source_name": source["name"],
                    "api": source["api"], "type_id": str(row.get("id")),
                    "type_name": str(row.get("name")), "pages": 3, "total": 60,
                    "posters": []}

        with patch("catalog.client_get_classes", new=fake_classes), \
                patch.object(catalog.catalog, "_probe", new=fake_probe):
            status = asyncio.run(catalog.catalog.refresh_routes(
                None, sources, split=True, force=True))
        self.assertEqual(len(status["routes"]), 12)
        # 12 个探测里有若干个是在「已有部分路由」之后才跑的
        self.assertTrue(any(n > 0 for n in seen))

    def test_class_rows_keeps_every_source_category(self):
        rows = catalog.class_rows({"class": [
            {"type_id": 1, "type_name": "动作片"},
            {"type_id": 2, "type_name": "国产剧"},
            {"type_id": 3, "type_name": "电影解说"},
            {"type_id": 4, "type_name": "Netflix"},
        ]})
        self.assertEqual([r["id"] for r in rows], ["1", "2", "3", "4"])
        self.assertEqual([r["junk"] for r in rows],
                         [False, False, True, False])
        self.assertEqual([r["mdb"] for r in rows],
                         ["Movie", "TV", "Mix", "Mix"])

    def test_selected_classes_honours_explicit_ids(self):
        cfg = config.get()
        cfg["vlib_class_ids"] = {"s_1": ["9"]}
        config.save(cfg)
        rows = vlib.selected_classes("s_1")
        self.assertEqual([r["id"] for r in rows], ["9"])
        self.assertTrue(all(r["junk"] for r in rows))
        self.assertIn("src:s_1:9",
                      [r["key"] for r in vlib.library_keys()])

    def test_load_classes_fetches_and_caches(self):
        catalog.catalog._classes.clear()

        async def fake_classes(client, source):
            return {"code": 1, "class": [
                {"type_id": 11, "type_name": "动作片"},
                {"type_id": 12, "type_name": "福利片"},
            ]}

        sources = [{"id": "s_1", "name": "源一",
                    "api": "https://a.invalid/api"}]
        with patch("catalog.client_get_classes", new=fake_classes):
            rows = asyncio.run(catalog.catalog.load_classes(None, sources))
        self.assertEqual([r["id"] for r in rows["s_1"]], ["11", "12"])
        self.assertTrue(rows["s_1"][1]["junk"])

    # ---- 清缓存 / 重建：库身份与路由不能互相拖死 ----
    def test_clear_cache_keeps_class_table_and_library_identity(self):
        """清缓存只清路由与分页，分类表保留 → 库 guid 仍能反查。"""
        guid = vlib.source_lib_guid("s_1", "1")
        catalog.catalog.reset()
        self.assertEqual(catalog.catalog._routes, {})
        self.assertEqual([r["id"] for r in catalog.catalog.classes("s_1")],
                         ["1", "2", "9"])
        self.assertEqual(vlib.source_lib_of(guid), ("s_1", "1"))
        self.assertTrue(vlib.is_virtual_lib(guid))

    def test_identity_survives_without_class_table(self):
        """分类表丢了（拉取失败）时，靠已建路由仍能反查库身份。"""
        guid = vlib.source_lib_guid("s_1", "2")
        catalog.catalog._classes = {}
        self.assertEqual(vlib.source_lib_of(guid), ("s_1", "2"))

    def test_rebuild_skips_classes_that_already_have_routes(self):
        """重建只补缺：已有路由的分类不再探测源站，避免限流把库打没。"""
        probed: list[str] = []

        async def fake_probe(client, source, row):
            probed.append(str(row["id"]))
            return {"source_id": source["id"], "source_name": source["name"],
                    "api": source["api"], "type_id": str(row["id"]),
                    "type_name": row["name"], "pages": 1, "total": 9,
                    "posters": []}

        async def fake_classes(client, source):
            return {"code": 1, "class": [
                {"type_id": 1, "type_name": "动作片"},
                {"type_id": 2, "type_name": "国产剧"},
            ]}

        sources = [{"id": "s_1", "name": "源一",
                    "api": "https://a.invalid/api"}]
        with patch("catalog.client_get_classes", new=fake_classes), \
                patch.object(catalog.catalog, "_probe", new=fake_probe):
            asyncio.run(catalog.catalog.refresh_routes(None, sources,
                                                       split=True))
        self.assertEqual(probed, [])
        self.assertEqual(catalog.catalog._routes["s_1:1"]["total"], 8)

    def test_partial_rebuild_keeps_untouched_routes(self):
        """部分分类探测失败时，旧路由保留，不会整库消失。"""

        async def fake_probe(client, source, row):
            if str(row["id"]) != "1":
                return None
            return {"source_id": source["id"], "source_name": source["name"],
                    "api": source["api"], "type_id": "1",
                    "type_name": row["name"], "pages": 1, "total": 100,
                    "posters": []}

        async def fake_classes(client, source):
            return {"code": 1, "class": [
                {"type_id": 1, "type_name": "动作片"},
                {"type_id": 2, "type_name": "国产剧"},
            ]}

        sources = [{"id": "s_1", "name": "源一",
                    "api": "https://a.invalid/api"},
                   {"id": "s_2", "name": "源二",
                    "api": "https://b.invalid/api"}]
        with patch("catalog.client_get_classes", new=fake_classes), \
                patch.object(catalog.catalog, "_probe", new=fake_probe):
            asyncio.run(catalog.catalog.refresh_routes(None, sources,
                                                       split=True, force=True))
        self.assertEqual(catalog.catalog._routes["s_1:1"]["total"], 100)
        self.assertEqual(catalog.catalog._routes["s_1:2"]["total"], 8)
        self.assertEqual(catalog.catalog._routes["s_2:3"]["total"], 8)


if __name__ == "__main__":
    unittest.main()
