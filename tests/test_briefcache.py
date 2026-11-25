import _bootstrap
import unittest

import httpx
from fastapi.testclient import TestClient

import app as appmod
import briefcache
import cms
import config


def make_title(vod_id: str = "browse-1", title: str = "浏览缓存"):
    return cms.OnlineTitle(
        provider="s_1", vod_id=vod_id, title=title, type_name="电影",
        lines={"m3u8": [cms.Episode("正片", "https://a.invalid/p.m3u8")]})


class BrowseCacheTest(unittest.TestCase):
    """虚拟库/搜索卡片在重启（升级安装）后仍要能点开详情。"""

    def setUp(self):
        self._sources = list(config.get().get("sources") or [])
        self._drop()
        briefcache.reset()

    def tearDown(self):
        cfg = config.get()
        cfg["sources"] = self._sources
        config.save(cfg)
        self._drop()
        briefcache.reset()

    @staticmethod
    def _drop():
        try:
            briefcache._FILE.unlink()
        except OSError:
            pass

    @staticmethod
    def _set_sources(enabled: bool = True):
        cfg = config.get()
        cfg["sources"] = [{"id": "s_1", "name": "源一",
                           "api": "https://a.invalid/api",
                           "enabled": enabled}]
        config.save(cfg)

    def test_browsed_brief_survives_restart(self):
        self._set_sources()
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([make_title()])[0]
            guid = brief["guid"]
            briefcache._write(force=True)
        # 退出上下文 = 进程停止；下面 store 重新构造，只能靠落盘缓存恢复。
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            restored = appmod.app.state.store.get(guid)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["title"], "浏览缓存")
            # 客户端缓存的卡片再点开，详情不该再透传官方（-6 媒体库不存在）。
            resp = client.get(f"/v/api/v1/item/{guid}")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("浏览缓存", resp.text)

    def test_disabled_source_is_not_restored(self):
        self._set_sources()
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember(
                [make_title("browse-2", "禁用源")])[0]
            guid = brief["guid"]
            briefcache._write(force=True)
        self._set_sources(enabled=False)
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            self.assertIsNone(appmod.app.state.store.get(guid))

    def test_cache_is_capped(self):
        briefcache.reset()
        rows = [{"guid": f"g{i}", "title": f"t{i}", "provider": "s_1"}
                for i in range(briefcache._MAX + 20)]
        briefcache.remember(rows)
        self.assertEqual(len(briefcache._items), briefcache._MAX)
        # 保留的是最新的那批
        self.assertIn(f"g{briefcache._MAX + 19}", briefcache._items)

    def test_persisted_brief_has_no_episode_list(self):
        """浏览缓存只落元数据：分集地址一条能到 173KB，800 条就 135MB。"""
        briefcache.reset()
        rows = [make_title("browse-slim"), make_title("browse-slim-2")]
        briefcache.remember(rows)
        for rec in briefcache._items.values():
            self.assertEqual(rec["brief"]["lines"], {})
            self.assertEqual(rec["brief"]["episodes"], 1)
        briefcache._write(force=True)
        text = briefcache._FILE.read_text(encoding="utf-8")
        self.assertNotIn("https://a.invalid/p.m3u8", text)

    def test_store_keeps_only_recent_episode_lists(self):
        """store 只对最近使用的少量条目保留 lines，其余卸成元数据 stub。"""
        store = cms.TitleStore()
        titles = [make_title(f"lru-{i}") for i in range(cms.RICH_MAX + 5)]
        rows = store.remember(titles)
        self.assertEqual(len(store._items), len(titles))
        rich = [g for g, b in store._items.items() if b.get("lines")]
        self.assertEqual(len(rich), cms.RICH_MAX)
        stub_guid = store.guid_of(titles[0])
        self.assertEqual(store.get(stub_guid)["lines"], {})
        # 补一次详情后重新成为「富条目」，不会把老条目挤爆。
        refreshed = dict(rows[0])
        refreshed["lines"] = {"m3u8": [cms.Episode("正片", "https://a.invalid/x.m3u8")]}
        store.put_brief(refreshed)
        self.assertTrue(store.get(stub_guid)["lines"])
        self.assertLessEqual(len(store._rich), cms.RICH_MAX)


class CmsEnsureLinesTest(unittest.IsolatedAsyncioTestCase):
    """轻量化的 CMS 条目重启后仍能按 vod_id 补回线路。"""

    def setUp(self):
        self._sources = list(config.get().get("sources") or [])
        cfg = config.get()
        cfg["sources"] = [{"id": "s_1", "name": "源",
                           "api": "https://a.invalid/api.php/provide/vod",
                           "enabled": True}]
        config.save(cfg)

    def tearDown(self):
        cfg = config.get()
        cfg["sources"] = self._sources
        config.save(cfg)

    async def test_cms_stub_refetches_lines(self):
        store = cms.TitleStore()
        stub = {"guid": "g-cms", "provider": "s_1", "vod_id": "42",
                "title": "测试", "lines": {}, "episodes": 1}

        def handler(request):
            return httpx.Response(200, json={
                "code": 1, "list": [{
                    "vod_id": 42, "vod_name": "测试",
                    "vod_play_from": "m3u8",
                    "vod_play_url": "第1集$https://a.invalid/1.m3u8",
                }]})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            got = await cms.ensure_lines(client, store, stub)
        self.assertTrue(got["lines"]["m3u8"])
        self.assertEqual(store.get("g-cms")["lines"]["m3u8"][0]["url"],
                         "https://a.invalid/1.m3u8")


if __name__ == "__main__":
    unittest.main()
