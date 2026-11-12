import _bootstrap
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

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


if __name__ == "__main__":
    unittest.main()
