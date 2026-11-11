import _bootstrap
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app as appmod
import cms
import config
import fake
import playstate
import playstate_api


def make_title():
    return cms.OnlineTitle(
        provider="s_1", vod_id="state-1", title="播放状态测试", type_name="电影",
        lines={"m3u8": [cms.Episode("正片", "https://a.invalid/p.m3u8")]})


async def empty_json(*_args, **_kwargs):
    return JSONResponse({"code": 0, "msg": "", "data": {"list": [], "total": 0}})


async def empty_list(*_args, **_kwargs):
    return JSONResponse({"code": 0, "msg": "", "data": []})


async def official_error(*_args, **_kwargs):
    return JSONResponse({"msg": "Not Found", "code": -6, "data": None})


class PlayStateTest(unittest.TestCase):
    def setUp(self):
        cfg = config.get()
        cfg["playstate"] = {}
        config.save(cfg)
        playstate._loaded = False
        playstate.load()

    def open_item(self, client):
        client.get("/v/online-admin")
        brief = appmod.app.state.store.remember([make_title()])[0]
        return brief["guid"], fake.media_guid(brief["guid"])

    def test_favorite_watched_and_item_fields(self):
        with TestClient(appmod.app) as client:
            guid, _ = self.open_item(client)
            resp = client.put("/v/api/v1/item/favorite", json={"guid": guid})
            self.assertEqual(resp.json()["code"], 0)
            client.post("/v/api/v1/item/watched", json={"guid": guid})
            detail = client.get(f"/v/api/v1/item/{guid}").json()["data"]
            self.assertEqual(detail["is_favorite"], 1)
            self.assertEqual(detail["is_watched"], 1)
            self.assertGreater(detail["watched_ts"], 0)
            self.assertIn(guid, config.get()["playstate"]["favorites"])
            client.request("DELETE", "/v/api/v1/item/favorite", json={"guid": guid})
            detail2 = client.get(f"/v/api/v1/item/{guid}").json()["data"]
            self.assertEqual(detail2["is_favorite"], 0)
            client.request("DELETE", "/v/api/v1/item/watched", json={"guid": guid})
            self.assertEqual(
                client.get(f"/v/api/v1/item/{guid}").json()["data"]["is_watched"], 0)

    def test_favorite_list_merges_online_cards(self):
        with TestClient(appmod.app) as client:
            guid, _ = self.open_item(client)
            playstate.add_favorite(guid, {"title": "播放状态测试"})
            with patch.object(playstate_api, "_passthrough", new=empty_json):
                resp = client.post("/v/api/v1/favorite/list", json={"page": 1, "page_size": 20})
            data = resp.json()["data"]
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["list"][0]["guid"], guid)
            self.assertEqual(data["list"][0]["is_favorite"], 1)

    def test_record_history_and_resume(self):
        with TestClient(appmod.app) as client:
            guid, mg = self.open_item(client)
            resp = client.post("/v/api/v1/play/record", json={
                "item_guid": guid, "ts": 120, "duration": 600,
                "media_guid": mg, "video_guid": "vg"})
            self.assertEqual(resp.json()["data"], True)
            with patch.object(playstate_api, "_passthrough", new=empty_list):
                history = client.get("/v/api/v1/play/list").json()["data"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["guid"], guid)
            self.assertEqual(history[0]["ts"], 120)
            self.assertEqual(history[0]["duration"], 600)
            info = client.post("/v/api/v1/play/info", json={"item_guid": guid}).json()["data"]
            self.assertEqual(info["ts"], 120)
            client.request("DELETE", "/v/api/v1/play/record", json={"item_guid": guid})
            with patch.object(playstate_api, "_passthrough", new=empty_list):
                self.assertEqual(client.get("/v/api/v1/play/list").json()["data"], [])

    def test_finished_item_leaves_history(self):
        with TestClient(appmod.app) as client:
            guid, _ = self.open_item(client)
            client.post("/v/api/v1/play/record", json={
                "item_guid": guid, "ts": 590, "duration": 600})
            with patch.object(playstate_api, "_passthrough", new=empty_list):
                self.assertEqual(client.get("/v/api/v1/play/list").json()["data"], [])
            self.assertEqual(
                client.get(f"/v/api/v1/item/{guid}").json()["data"]["is_watched"], 1)

    def test_more_menu_noop_and_official_passthrough(self):
        with TestClient(appmod.app) as client:
            guid, _ = self.open_item(client)
            self.assertEqual(
                client.get(f"/v/api/v1/media/itemfile/{guid}").json()["code"], 0)
            self.assertEqual(
                client.post("/v/api/v1/item/refresh", json={"guid": guid}).json()["code"], 0)
            with patch.object(playstate_api, "_passthrough", new=official_error):
                self.assertEqual(
                    client.put("/v/api/v1/item/favorite",
                               json={"guid": "0" * 32}).json()["code"], -6)


if __name__ == "__main__":
    unittest.main()
