"""源被禁用后，旧线路必须立刻失效（不能继续从禁用源取流）。"""
import _bootstrap  # noqa: F401
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app as appmod
import catalog
import charts
import cms
import config
import fake
import vlib


def src(sid: str, enabled: bool = True) -> dict:
    return {"id": sid, "name": sid, "enabled": enabled,
            "api": f"https://{sid}.invalid/api.php/provide/vod/"}


def chart_item(cat: str, index: int) -> dict:
    douban_id = f"{cat}-{index}"
    return {"id": douban_id, "guid": charts.chart_guid(cat, douban_id),
            "cat": cat, "title": f"榜单{cat}{index}", "rating": 8.4,
            "cover": f"https://img.invalid/{douban_id}.jpg", "year": "2025",
            "subtitle": "2025 / 中国", "desc": "简介",
            "tags": ["剧情"], "origin": "douban-tag"}


def seed_charts() -> None:
    data = {"updated_at": int(time.time()), "provider_ok": 1,
            "provider_total": 1,
            "charts": {"movie": [chart_item("movie", i) for i in range(2)]}}
    charts.chart_cache._apply(data)


def publish(sources: list[dict]) -> None:
    cfg = config.get()
    cfg["sources"] = sources
    config.save(cfg)


class SourceLiveTest(unittest.TestCase):
    def setUp(self):
        self._orig = [dict(s) for s in config.get().get("sources", [])]
        publish([src("s_old", True), src("s_new", True)])

    def tearDown(self):
        publish(self._orig)

    def test_enabled_and_disabled(self):
        self.assertTrue(config.source_live("s_old"))
        publish([src("s_old", False), src("s_new", True)])
        self.assertFalse(config.source_live("s_old"))
        self.assertTrue(config.source_live("s_new"))

    def test_unknown_provider_is_not_killed(self):
        # 配置里没有这个 id（历史数据/外部构造），保持可用，避免误杀。
        self.assertTrue(config.source_live("s_ghost"))
        self.assertTrue(config.source_live(""))

    def test_brief_from_disabled_source_is_hidden(self):
        title = cms.OnlineTitle(
            provider="s_old", vod_id="old-1", title="旧源影片", type_name="电影",
            lines={"lz": [cms.Episode("正片", "https://cdn.invalid/1.m3u8")]})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            store = appmod.app.state.store
            brief = store.remember([title])[0]
            guid = brief["guid"]
            self.assertIsNotNone(appmod._brief(store, guid))
            publish([src("s_old", False), src("s_new", True)])
            self.assertIsNone(appmod._brief(store, guid))
            # 播放引用也随之中断：不会再从禁用源取流。
            self.assertIsNone(
                appmod._play_ref(store, fake.media_guid(guid)))
            self.assertIsNone(
                appmod._play_ref(store, fake.episode_guid(guid, 0)))

    def test_catalog_route_hidden_when_source_disabled(self):
        catalog.catalog._routes = {"movie": {
            "source_id": "s_old", "source_name": "旧源", "type_id": 1,
            "type_name": "电影", "total": 500, "pages": 10}}
        catalog.catalog._updated_at = time.time()
        self.assertIsNotNone(catalog.catalog.route("movie"))
        publish([src("s_old", False), src("s_new", True)])
        self.assertIsNone(catalog.catalog.route("movie"))
        self.assertTrue(catalog.catalog.needs_rebuild("movie"))
        catalog.catalog.reset()


class RebindTest(unittest.TestCase):
    """榜单条目绑在旧源上时，禁用后应改用新启用源重绑。"""

    def setUp(self):
        self._orig = [dict(s) for s in config.get().get("sources", [])]
        publish([src("s_old", True), src("s_new", True)])
        cfg = config.get()
        cfg.update({"vlib_enabled": True, "vlib_charts": True,
                    "vlib_catalog": True, "vlib_refresh_hours": 24,
                    "enabled_limit": 10})
        config.save(cfg)
        seed_charts()
        vlib._BINDS.clear()
        vlib._FAILED.clear()

    def tearDown(self):
        charts.chart_cache.reset()
        vlib._BINDS.clear()
        vlib._FAILED.clear()
        publish(self._orig)

    def test_disabled_binding_is_rebound_to_new_source(self):
        cat = "movie"
        meta = charts.chart_cache.items(cat)[0]
        guid = meta["guid"]
        old_title = cms.OnlineTitle(
            provider="s_old", vod_id="old-9", title=meta["title"],
            type_name="电影", year="2025",
            lines={"lz": [cms.Episode("正片", "https://old.invalid/1.m3u8")]})
        store = cms.TitleStore()
        old_brief = fake.brief_of(old_title, guid)
        store.put_brief(old_brief)
        vlib._remember_bind(guid, old_brief)

        publish([src("s_old", False), src("s_new", True)])
        new_title = cms.OnlineTitle(
            provider="s_new", vod_id="new-9", title=meta["title"],
            type_name="电影", year="2025",
            lines={"lz": [cms.Episode("正片", "https://new.invalid/1.m3u8")]})
        with patch("cms.search_all", new=AsyncMock(return_value=[new_title])):
            brief = asyncio.run(vlib.ensure_bound(store, None, guid))
        self.assertIsNotNone(brief)
        self.assertEqual(brief["provider"], "s_new")
        self.assertEqual(store.get(guid)["provider"], "s_new")
        # 旧绑定记录也应被覆盖，重启后不会再回到旧源
        self.assertEqual(vlib._BINDS[guid]["brief"]["provider"], "s_new")

    def test_restore_drops_bindings_of_disabled_sources(self):
        cat = "movie"
        meta = charts.chart_cache.items(cat)[1]
        guid = meta["guid"]
        old_title = cms.OnlineTitle(
            provider="s_old", vod_id="old-10", title=meta["title"],
            type_name="电影", year="2025",
            lines={"lz": [cms.Episode("正片", "https://old.invalid/2.m3u8")]})
        store = cms.TitleStore()
        vlib._remember_bind(guid, fake.brief_of(old_title, guid))
        publish([src("s_old", False), src("s_new", True)])
        vlib.restore(store)
        self.assertIsNone(store.get(guid))
        self.assertNotIn(guid, vlib._BINDS)


if __name__ == "__main__":
    unittest.main()
