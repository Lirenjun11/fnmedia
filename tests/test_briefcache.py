import _bootstrap
import unittest

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


if __name__ == "__main__":
    unittest.main()
