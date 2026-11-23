import _bootstrap  # noqa: F401 - 统一临时 state 目录与 sys.path

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import acl
import app as appmod
import cms
import fake
import skipmarks


def make_tv():
    return cms.OnlineTitle(
        provider="s_1", vod_id="skip-tv", title="跳片测试剧", type_name="国产剧",
        lines={"m3u8": [cms.Episode(f"第{i + 1}集", f"https://a.invalid/e{i}.m3u8")
                        for i in range(3)]})


def make_movie():
    return cms.OnlineTitle(
        provider="s_1", vod_id="skip-movie", title="跳片测试电影", type_name="电影",
        lines={"m3u8": [cms.Episode("正片", "https://a.invalid/m.m3u8")]})


class SkipMarkStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(
            skipmarks, "_STATE", Path(self.tmp.name) / "skip_marks.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_set_read_and_disable_keeps_seconds(self):
        self.assertEqual(skipmarks.play_config("tv-1"),
                         {"guid": "tv-1", "skip_opening": 0, "skip_ending": 0})
        skipmarks.set_mark("tv-1", {"skip_opening": 85, "skip_ending": 100})
        self.assertEqual(skipmarks.play_config("tv-1"),
                         {"guid": "tv-1", "skip_opening": 85, "skip_ending": 100})
        # 关闭只是下发 0，不丢已设秒数；重新打开即恢复
        skipmarks.set_mark("tv-1", {"enabled": False})
        self.assertEqual(skipmarks.play_config("tv-1"),
                         {"guid": "tv-1", "skip_opening": 0, "skip_ending": 0})
        self.assertEqual(skipmarks.get("tv-1")["skip_opening"], 85)
        skipmarks.set_mark("tv-1", {"enabled": True})
        self.assertEqual(skipmarks.play_config("tv-1")["skip_opening"], 85)

    def test_seconds_are_clamped(self):
        skipmarks.set_mark("tv-1", {"skip_opening": 9999, "skip_ending": -5})
        mark = skipmarks.get("tv-1")
        self.assertEqual(mark["skip_opening"], skipmarks.MAX_SKIP_SECONDS)
        self.assertEqual(mark["skip_ending"], 0)
        skipmarks.set_mark("tv-1", {"skip_opening": "90"})
        self.assertEqual(skipmarks.get("tv-1")["skip_opening"], 90)

    def test_marks_are_per_account(self):
        token = acl.push(acl.Identity(uid="u1"))
        try:
            skipmarks.set_mark("tv-1", {"skip_opening": 85})
        finally:
            acl.reset(token)
        token = acl.push(acl.Identity(uid="u2"))
        try:
            self.assertEqual(skipmarks.play_config("tv-1")["skip_opening"], 0)
        finally:
            acl.reset(token)
        token = acl.push(acl.Identity(uid="u1"))
        try:
            self.assertEqual(skipmarks.play_config("tv-1")["skip_opening"], 85)
        finally:
            acl.reset(token)

    def test_alias_key_is_readable(self):
        season = fake.season_guid("tv-1")
        # 客户端把标记写在季 guid 上（官方 NAS 端就是按季保存）
        skipmarks.set_mark(season, {"skip_opening": 70})
        self.assertEqual(skipmarks.play_config("tv-1", season)["skip_opening"], 70)
        skipmarks.delete("tv-1", season)
        self.assertEqual(skipmarks.play_config("tv-1", season)["skip_opening"], 0)


class SkipMarkPlayInfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(
            skipmarks, "_STATE", Path(self.tmp.name) / "skip_marks.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_episodes_share_one_mark_and_movie_uses_own_guid(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            tv = appmod.app.state.store.remember([make_tv()])[0]
            movie = appmod.app.state.store.remember([make_movie()])[0]
            skipmarks.set_mark(tv["guid"], {"skip_opening": 85, "skip_ending": 110})
            for index in (0, 1, 2):
                info = fake.episode_play_info(tv, index,
                                              fake.episode_guid(tv["guid"], index))
                self.assertEqual(info["play_config"],
                                 {"guid": tv["guid"], "skip_opening": 85,
                                  "skip_ending": 110})
            self.assertEqual(fake.play_info(movie, fake.media_guid(movie["guid"]),
                                            fake.video_guid(movie["guid"]),
                                            movie["guid"])["play_config"],
                             {"guid": movie["guid"], "skip_opening": 0,
                              "skip_ending": 0})

    def test_play_info_response_carries_play_config(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            tv = appmod.app.state.store.remember([make_tv()])[0]
            eg = fake.episode_guid(tv["guid"], 1)
            resp = client.post("/v/api/v1/play/setConfigByItem",
                               json={"guid": eg, "skip_opening": 90,
                                     "skip_ending": 120})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            # 写操作的响应用官方「成功」的最小形状 {code:0, msg:"", data:null}：
            # 同一个播放器模块的 play/record 就是这份，客户端从不报错；带内容的
            # data 反而被拒过两次（先 {guid,play_config} 再平铺 skip_*），都会弹
            # 「影视服务出错，请检查服务」而服务端其实写成功了。
            self.assertEqual(body["code"], 0)
            self.assertIsNone(body["data"])
            # 秒数本身仍然要落到存储层（响应体不携带，靠 play/info 下发）
            mark = skipmarks.get(tv["guid"])
            self.assertEqual(mark["skip_opening"], 90)
            info = client.post("/v/api/v1/play/info",
                               json={"item_guid": eg}).json()["data"]
            self.assertEqual(info["play_config"]["skip_opening"], 90)
            self.assertEqual(info["play_config"]["skip_ending"], 120)
            # 另一集同样读到同一份标记
            other = fake.episode_guid(tv["guid"], 2)
            info2 = client.post("/v/api/v1/play/info",
                                json={"item_guid": other}).json()["data"]
            self.assertEqual(info2["play_config"]["skip_opening"], 90)

    def test_ext_read_and_delete(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            tv = appmod.app.state.store.remember([make_tv()])[0]
            eg = fake.episode_guid(tv["guid"], 0)
            client.post("/v/api/v1/play/setConfigByItem",
                        json={"guid": eg, "skip_opening": 60})
            got = client.get("/v/api/ext/skip-marks",
                             params={"guid": eg}).json()["data"]
            self.assertEqual(got["guid"], tv["guid"])
            self.assertEqual(got["mark"]["skip_opening"], 60)
            self.assertEqual(got["play_config"]["skip_opening"], 60)
            client.request("DELETE", "/v/api/ext/skip-marks", params={"guid": eg})
            got = client.get("/v/api/ext/skip-marks",
                             params={"guid": eg}).json()["data"]
            self.assertEqual(got["play_config"]["skip_opening"], 0)

    def test_missing_guid_is_rejected(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            resp = client.post("/v/api/v1/play/setConfigByItem",
                               json={"skip_opening": 60})
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
