import _bootstrap
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app as appmod
import cms
import config
import playcache
import playstate
import playstate_api


def make_title(vod_id: str = "cache-1", title: str = "缓存测试"):
    return cms.OnlineTitle(
        provider="s_1", vod_id=vod_id, title=title, type_name="电影",
        lines={"m3u8": [cms.Episode("正片", "https://a.invalid/p.m3u8")]})


async def empty_list(*_args, **_kwargs):
    return JSONResponse({"code": 0, "msg": "", "data": []})


class PlayCacheTest(unittest.TestCase):
    def setUp(self):
        self._cfg = config.get()
        self._orig_sources = list(self._cfg.get("sources") or [])
        cfg = config.get()
        cfg["playstate"] = {}
        config.save(cfg)
        playstate.reset()
        playstate.load()
        self._drop_file()
        playcache.reset()

    def tearDown(self):
        cfg = config.get()
        cfg["sources"] = self._orig_sources
        cfg["playstate"] = {}
        config.save(cfg)
        playstate.reset()
        playstate.load()
        self._drop_file()
        playcache.reset()

    @staticmethod
    def _drop_file():
        try:
            playcache._FILE.unlink()
        except OSError:
            pass

    def test_history_survives_service_restart(self):
        """重启（升级安装）后 store 为空，最近播放仍要能列出来。"""
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([make_title()])[0]
            guid = brief["guid"]
            client.post("/v/api/v1/play/record", json={
                "item_guid": guid, "ts": 120, "duration": 600})
        # 退出上面的上下文 = 进程停止；下面是新进程，store 重新构造，
        # 只能靠 state/play_titles.json 恢复出列表。
        with TestClient(appmod.app) as client:
            with patch.object(playstate_api, "_passthrough", new=empty_list):
                history = client.get("/v/api/v1/play/list").json()["data"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["guid"], guid)
            self.assertEqual(history[0]["ts"], 120)

    def test_restore_needs_playstate_reference(self):
        store = cms.TitleStore()
        brief = store.remember([make_title()])[0]
        playcache.remember(brief)
        playcache.reset()
        # 没有 playstate 记录引用它（例如用户删了历史）→ 不恢复
        self.assertEqual(playcache.restore(cms.TitleStore()), 0)

    def test_restore_skips_disabled_source(self):
        cfg = config.get()
        cfg["sources"] = [{"id": "s_1", "name": "源一",
                           "api": "https://a.invalid/api", "enabled": False}]
        config.save(cfg)
        store = cms.TitleStore()
        brief = store.remember([make_title()])[0]
        playstate.record({"item_guid": brief["guid"], "ts": 10}, brief)
        playcache.remember(brief)
        playcache.reset()
        fresh = cms.TitleStore()
        self.assertEqual(playcache.restore(fresh), 0)
        self.assertIsNone(fresh.get(brief["guid"]))

    def test_ensure_backfills_evicted_store_item(self):
        store = cms.TitleStore()
        brief = store.remember([make_title()])[0]
        playcache.remember(brief)
        playstate.record({"item_guid": brief["guid"], "ts": 30}, brief)
        fresh = cms.TitleStore()
        self.assertTrue(playcache.ensure(fresh, brief["guid"]))
        self.assertIsNotNone(fresh.get(brief["guid"]))
        # 已存在时不重复注册
        self.assertFalse(playcache.ensure(fresh, brief["guid"]))

    def test_cache_is_capped(self):
        for i in range(playcache._MAX + 5):
            playcache.remember({"guid": f"g{i}", "provider": "s_1",
                                "title": f"t{i}", "lines": {}, "is_tv": False})
        self.assertEqual(playcache.count(), playcache._MAX)


if __name__ == "__main__":
    unittest.main()
