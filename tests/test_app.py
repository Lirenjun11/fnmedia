import _bootstrap  
import asyncio
import json
import os
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import hls


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self):  
        payload = json.dumps({
            "code": 0, "msg": "",
            "data": [{"guid": "local-1", "title": "本地影片"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


_server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
threading.Thread(target=_server.serve_forever, daemon=True).start()
os.environ["FNOS_MEDIA_UPSTREAM_URL"] = f"http://127.0.0.1:{_server.server_address[1]}"

from fastapi.testclient import TestClient  
import app as appmod  
import config  


class AppContractTest(unittest.TestCase):
    def setUp(self):
        hls._PLAYLISTS.clear()
        hls._META.clear()

    @classmethod
    def tearDownClass(cls):
        config.save({**config.get(), "sources": [], "subscriptions": []})

    def test_healthz_ok_with_zero_sources(self):
        with TestClient(appmod.app) as client:
            resp = client.get("/_ext/healthz")
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])
            self.assertEqual(resp.json()["sources"], 0)

    def test_bootstrap_reports_pure_passthrough(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            data = client.get("/v/api/ext/bootstrap").json()["data"]
            self.assertEqual(data["sources_total"], 0)
            self.assertFalse(data["online_enabled"])
            self.assertNotIn("admin_token", data["config"])

    def test_zero_source_search_is_verbatim_upstream(self):
        with TestClient(appmod.app) as client:
            data = client.get("/v/api/v1/search/list?q=anything").json()
            self.assertEqual(len(data["data"]), 1)
            self.assertEqual(data["data"][0]["guid"], "local-1")

    def test_admin_page_serves_static_and_token_cookie(self):
        with TestClient(appmod.app) as client:
            page = client.get("/v/online-admin")
            self.assertEqual(page.status_code, 200)
            self.assertIn("fnos_media_ext", page.text)
            self.assertIn('name="fnos-token"', page.text)
            self.assertIn("fnos_media_admin", page.cookies)
            import re
            match = re.search(r'src="(/v/ext/assets/[^"]+\.js)"', page.text)
            self.assertIsNotNone(match)
            assets = client.get(match.group(1))
            self.assertEqual(assets.status_code, 200)
            self.assertIn("/v/api/ext", assets.text)

    def test_write_requires_cookie_then_succeeds(self):
        with TestClient(appmod.app) as client:
            denied = client.patch("/v/api/ext/config", json={"search_timeout": 2.0})
            self.assertEqual(denied.status_code, 401)
            client.get("/v/online-admin")
            allowed = client.patch("/v/api/ext/config", json={"search_timeout": 2.0})
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json()["data"]["config"]["search_timeout"], 2.0)

    def test_batch_enable_disable_delete(self):
        payload = json.dumps({"api_site": {
            "a.invalid": {"name": "A", "api": "https://a.invalid/api.php/provide/vod"},
            "b.invalid": {"name": "B", "api": "https://b.invalid/api.php/provide/vod"}}})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            client.post("/v/api/ext/subscriptions/fetch", json={"text": payload})
            ids = [s["id"] for s in
                   client.get("/v/api/ext/bootstrap").json()["data"]["sources"]]
            self.assertEqual(len(ids), 2)
            r = client.post("/v/api/ext/sources/batch",
                            json={"ids": ids, "action": "disable"})
            self.assertEqual(r.json()["data"]["changed"], 2)
            r = client.post("/v/api/ext/sources/batch",
                            json={"ids": ids[:1], "action": "enable"})
            self.assertEqual(r.json()["data"]["changed"], 1)
            r = client.post("/v/api/ext/sources/batch",
                            json={"ids": ids, "action": "delete"})
            self.assertEqual(r.json()["data"]["total"], 0)

    def test_person_list_route(self):
        import cms
        title = cms.OnlineTitle(
            provider="s_1", vod_id="77", title="演职员测试", type_name="电视剧",
            director="张三、李四", actor="王五,赵六",
            lines={"m3u8": [cms.Episode("第1集", "https://a.invalid/1.m3u8")]})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            resp = client.post(f"/v/api/v1/person/list/{brief['guid']}")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            self.assertEqual(data["total"], 4)
            self.assertEqual(data["list"][0]["name"], "张三")
            self.assertEqual(data["list"][0]["job"], "Director")
            self.assertEqual(data["list"][2]["name"], "王五")
            episode = data["list"][2]["item_guid"]

    def test_stream_post_backfills_meta(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="103", title="流信息回填", type_name="电影",
            lines={"m3u8": [cms_mod.Episode("正片", "https://a.invalid/1.m3u8")]})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            guid = brief["guid"]
            mg = fake.media_guid(guid)
            hls.remember_playlist_meta((guid, 0, 0),
                                       {"width": 1280, "height": 720,
                                        "bandwidth": 5000000})
            resp = client.post("/v/api/v1/stream", json={"media_guid": mg})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            video = data["video_stream"]
            self.assertEqual(video["resolution_type"], "720p")
            self.assertEqual(video["bps"], 5000000)
            self.assertEqual(data["file_stream"]["guid"], mg)
            self.assertEqual(data["audio_streams"], [])
            self.assertEqual(data["subtitle_streams"], [])
            self.assertIsNone(data["cloud_storage_info"])
            self.assertEqual(len(data["qualities"]), 1)
            self.assertEqual(data["qualities"][0]["resolution"], "720p")
            self.assertEqual(data["qualities"][0]["bitrate"], 5000000)

    def test_line_switch_contract(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="104", title="换线契约",
            provider_name="本地", type_name="电视剧",
            lines={
                "line-a": [cms_mod.Episode("第1集", "https://a.invalid/1.m3u8"),
                           cms_mod.Episode("第2集", "https://a.invalid/2.m3u8")],
                "line-b": [cms_mod.Episode("第1集", "https://b.invalid/1.m3u8"),
                           cms_mod.Episode("第2集", "https://b.invalid/2.m3u8")],
                "line-c": [cms_mod.Episode("第1集", "https://c.invalid/1.m3u8"),
                           cms_mod.Episode("第2集", "https://c.invalid/2.m3u8")],
            })
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            guid = brief["guid"]
            eg = fake.episode_guid(guid, 1)
            mg = fake.ep_media_guid(eg)
            self.assertEqual(fake.line_index(fake.line_code("line-b"),
                                             ["line-a", "line-b", "line-c"]), 1)
            self.assertEqual(fake.line_index("L2",
                                             ["line-a", "line-b", "line-c"]), 2)
            self.assertIsNone(fake.line_index(fake.line_code("gone"),
                                              ["line-a", "line-b"]))
            sl = client.get(f"/v/api/v1/stream/list/{eg}").json()["data"]
            self.assertEqual(len(sl["qualities"]), 3)
            self.assertEqual(sl["qualities"][1]["resolution"], "本地：line-b")
            self.assertTrue(sl["qualities"][1]["progressive"])
            self.assertFalse(sl["qualities"][1]["is_m3u8"])
            self.assertEqual(brief["provider_name"], "本地")
            code = sl["qualities"][1]["bitrate"]
            posted = client.post("/v/api/v1/stream/list",
                                 json={"item_guid": eg}).json()["data"]
            self.assertEqual(len(posted["qualities"]), 3)
            switched = client.post("/v/api/v1/stream", json={
                "media_guid": mg, "bitrate": str(code)}).json()["data"]
            self.assertEqual(switched["video_stream"]["bps"], code)
            self.assertEqual(switched["video_stream"]["resolution_type"],
                             "本地：line-b")
            legacy = client.post("/v/api/v1/stream", json={
                "media_guid": mg, "bitrate": "L2"}).json()["data"]
            self.assertEqual(legacy["video_stream"]["resolution_type"],
                             "本地：line-c")
            by_label = client.post("/v/api/v1/stream", json={
                "media_guid": mg, "line": "本地：line-c"}).json()["data"]
            self.assertEqual(by_label["video_stream"]["resolution_type"],
                             "本地：line-c")
            played = client.post("/v/api/v1/play/play", json={
                "media_guid": mg, "bitrate": code}).json()["data"]
            self.assertIn("preset.m3u8", played["play_link"])
            self.assertIn("line=1", played["play_link"])
            self.assertNotIn("auto=1", played["play_link"])
            async def fake_fetch(_client, url, timeout=3.0, depth=0):
                return ("#EXTM3U\n#EXTINF:1.0,\ns.ts\n", url,
                        {"width": 0, "height": 0, "bandwidth": 0})
            with patch.object(hls, "ensure_public_target", new=lambda u: u), \
                    patch.object(hls, "fetch_playlist", new=fake_fetch):
                master = client.get(played["play_link"])
                self.assertEqual(master.status_code, 200)
                self.assertEqual(master.text.count("#EXT-X-STREAM-INF"), 3)
                self.assertIn('NAME="本地：line-b"', master.text)
                self.assertIn("main.m3u8?line=1&index=1", master.text)
                self.assertTrue(master.text.split('NAME="本地：line-b"')[0]
                                .endswith("BANDWIDTH=9000000,PROGRAM-ID=1,"))
                auto = client.post("/v/api/v1/play/play",
                                   json={"media_guid": mg}).json()["data"]
                self.assertIn("auto=1", auto["play_link"])
                auto_master = client.get(auto["play_link"])
            self.assertEqual(auto_master.text.count("#EXT-X-STREAM-INF"), 3)
            self.assertIn("main.m3u8?line=0&index=1", auto_master.text)

    def test_share_page_treated_as_hls(self):
        import cms as cms_mod
        import fake
        self.assertFalse(appmod._is_direct_media("https://v.x/share/abc"))
        self.assertFalse(appmod._is_direct_media("https://v.x/a.m3u8?sign=1"))
        self.assertTrue(appmod._is_direct_media("https://v.x/a.mp4?x=1"))
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="108", title="分享页线路", type_name="电影",
            lines={"lz": [cms_mod.Episode("正片", "https://v.x/share/abc")],
                   "tp": [cms_mod.Episode("正片", "https://v.x/a.m3u8")]})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            guid = brief["guid"]
            mg = fake.media_guid(guid)
            data = client.post("/v/api/v1/play/play",
                               json={"media_guid": mg}).json()["data"]
            self.assertIn("preset.m3u8", data["play_link"])
            self.assertEqual(data["wrapper"], "HLS")
            range_resp = client.get(f"/v/api/v1/media/range/{mg}",
                                    follow_redirects=False)
            self.assertEqual(range_resp.status_code, 302)
            self.assertIn("preset.m3u8", range_resp.headers["location"])

    def test_episode_detail_and_play_info(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="106", title="单集详情", type_name="电视剧",
            poster="https://img.invalid/p.jpg",
            lines={"lz": [cms_mod.Episode("第1集", "https://a.invalid/1.m3u8"),
                          cms_mod.Episode("第2集", "https://a.invalid/2.m3u8")]})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            guid = brief["guid"]
            eg = fake.episode_guid(guid, 1)
            sg = fake.season_guid(guid)
            season = client.get(f"/v/api/v1/item/{sg}").json()["data"]
            self.assertEqual(season["type"], "Season")
            episode = client.get(f"/v/api/v1/item/{eg}").json()["data"]
            self.assertEqual(episode["type"], "Episode")
            self.assertEqual(episode["guid"], eg)
            self.assertEqual(episode["parent_guid"], sg)
            self.assertEqual(episode["episode_number"], 2)
            self.assertEqual(episode["media_guid"], fake.ep_media_guid(eg))
            self.assertTrue(episode["posters"].startswith("/online/"))
            self.assertEqual(episode["backdrops"], episode["posters"])
            phone = {"User-Agent": "fnos-media/1.8.0 (Android 14; Phone)"}
            mobile_seasons = client.get(f"/v/api/v1/season/list/{guid}",
                                        headers=phone).json()["data"]
            self.assertEqual(mobile_seasons[0]["number_of_episodes"], 2)
            web_seasons = client.get(
                f"/v/api/v1/season/list/{guid}",
                headers={"User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36"},
            ).json()["data"]
            self.assertEqual(web_seasons[0]["number_of_episodes"], 0)
            query = client.get(
                f"/v/api/v1/item/detail?guid={eg}").json()["data"]
            self.assertEqual(query["guid"], eg)
            listed = client.post("/v/api/v1/item/list", json={
                "parent_guid": sg}).json()["data"]
            self.assertEqual(listed["total"], 2)
            self.assertEqual(listed["list"][1]["guid"], eg)
            by_media = client.get(
                f"/v/api/v1/episode/list/{fake.ep_media_guid(eg)}").json()["data"]
            self.assertEqual(len(by_media), 2)
            title_by_media = client.get(
                f"/v/api/v1/item/{fake.media_guid(guid)}").json()["data"]
            self.assertEqual(title_by_media["guid"], guid)
            self.assertEqual(title_by_media["type"], "TV")
            info = client.post("/v/api/v1/play/info",
                               json={"item_guid": eg}).json()["data"]
            self.assertEqual(info["type"], "Episode")
            self.assertEqual(info["guid"], eg)
            self.assertEqual(info["parent_guid"], sg)
            self.assertEqual(info["item"]["guid"], eg)
            self.assertEqual(info["media_guid"], fake.ep_media_guid(eg))
            self.assertEqual(info["video_guid"], fake.ep_video_guid(eg))

    def test_web_and_mobile_payload_compatibility(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="107", title="双端兼容", type_name="电视剧",
            provider_name="本地",
            lines={"lz": [cms_mod.Episode("第1集", "https://a.invalid/1.m3u8"),
                          cms_mod.Episode("第2集", "https://a.invalid/2.m3u8")],
                   "tp": [cms_mod.Episode("第1集", "https://b.invalid/1.m3u8"),
                          cms_mod.Episode("第2集", "https://b.invalid/2.m3u8")]})
        agents = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36",
            "fnos-media/1.8.0 (Android 14; Phone) okhttp/4.12.0",
            "1.5.2 (com.trim.tv; build:1) okHttp/4.9.0",
        )
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            guid = brief["guid"]
            eg = fake.episode_guid(guid, 0)
            mg = fake.ep_media_guid(eg)
            for ua in agents:
                headers = {"User-Agent": ua}
                detail = client.get(f"/v/api/v1/item/{eg}",
                                    headers=headers).json()["data"]
                for key in ("guid", "type", "parent_guid", "season_number",
                            "episode_number", "media_guid", "video_guid",
                            "can_play", "play_item_guid"):
                    self.assertIn(key, detail, f"{ua}: {key}")
                self.assertEqual(detail["type"], "Episode")
                info = client.post("/v/api/v1/play/info",
                                   json={"item_guid": eg},
                                   headers=headers).json()["data"]
                self.assertEqual(info["type"], "Episode")
                self.assertEqual(info["item"]["guid"], eg)
                episodes = client.get(f"/v/api/v1/episode/list/{eg}",
                                      headers=headers).json()["data"]
                self.assertEqual(len(episodes), 2)
                self.assertEqual(episodes[0]["guid"], eg)
                for key in ("parent_guid", "season_number", "episode_number",
                            "media_guid", "video_guid", "can_play",
                            "play_item_guid"):
                    self.assertIn(key, episodes[0], f"{ua}: episode {key}")
                played = client.post("/v/api/v1/play/play",
                                     json={"media_guid": mg,
                                           "bitrate": fake.line_code("lz")},
                                     headers=headers).json()["data"]
                async def fake_fetch(_client, url, timeout=3.0, depth=0):
                    return ("#EXTM3U\n#EXTINF:1.0,\ns.ts\n", url,
                            {"width": 0, "height": 0, "bandwidth": 0})
                with patch.object(hls, "ensure_public_target",
                                  new=lambda u: u), \
                        patch.object(hls, "fetch_playlist", new=fake_fetch):
                    master = client.get(played["play_link"], headers=headers)
                self.assertEqual(master.status_code, 200)
                self.assertEqual(master.text.count("#EXT-X-STREAM-INF"), 2)
                self.assertIn('NAME="本地：lz"', master.text)

    def test_probe_requires_keyword(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            r = client.post("/v/api/ext/sources/probe",
                            json={"ids": [], "keyword": ""})
            self.assertEqual(r.status_code, 400)

    def test_probe_without_sources_returns_empty(self):
        config.save({**config.get(), "sources": []})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            r = client.post("/v/api/ext/sources/probe",
                            json={"ids": [], "keyword": "测试"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["data"]["results"], [])

    def test_probe_covers_all_sources(self):
        import cms
        config.save({**config.get(), "sources": [
            {"id": "s_a", "name": "A", "api": "https://a.invalid/api.php/provide/vod",
             "enabled": True, "from": "manual"},
            {"id": "s_b", "name": "B", "api": "https://b.invalid/api.php/provide/vod",
             "enabled": True, "from": "manual"},
        ]})

        async def instant(*_args, **_kwargs):
            return []

        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            with patch.object(cms, "search_source", new=instant):
                resp = client.post("/v/api/ext/sources/probe",
                                   json={"ids": [], "keyword": "测试"})
            data = resp.json()["data"]
            self.assertEqual(data["total"], 2)
            self.assertEqual(data["done"], 2)
            self.assertEqual(data["timed_out"], 0)
        config.save({**config.get(), "sources": []})

    def test_probe_budget_returns_partial_results(self):
        import admin_api
        import cms
        config.save({**config.get(), "sources": [
            {"id": f"s_{i}", "name": str(i),
             "api": f"https://s{i}.invalid/api.php/provide/vod",
             "enabled": True, "from": "manual"} for i in range(3)]})

        async def slow(*_args, **_kwargs):
            await asyncio.sleep(5)
            return []

        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            with patch.object(cms, "search_source", new=slow), \
                    patch.object(admin_api, "PROBE_BUDGET", 0.1):
                resp = client.post("/v/api/ext/sources/probe",
                                   json={"ids": [], "keyword": "测试"})
            data = resp.json()["data"]
            self.assertEqual(data["total"], 3)
            self.assertGreater(data["timed_out"], 0)
            self.assertLess(data["done"], 3)
        config.save({**config.get(), "sources": []})

    def test_write_allows_mismatched_origin_behind_proxy(self):
        import re
        with TestClient(appmod.app) as client:
            page = client.get("/v/online-admin")
            token = re.search(r'name="fnos-token" content="([^"]+)"', page.text).group(1)
            resp = client.patch(
                "/v/api/ext/config",
                json={"search_timeout": 2.2},
                headers={"X-Admin-Token": token,
                         "Origin": "https://another-host.invalid:8443"},
            )
            self.assertEqual(resp.status_code, 200)

    def test_write_with_header_token_without_cookie(self):
        import re
        with TestClient(appmod.app) as client:
            page = client.get("/v/online-admin")
            match = re.search(r'name="fnos-token" content="([^"]+)"', page.text)
            self.assertIsNotNone(match)
            client.cookies.clear()
            allowed = client.patch(
                "/v/api/ext/config",
                json={"search_timeout": 1.8},
                headers={"X-Admin-Token": match.group(1)},
            )
            self.assertEqual(allowed.status_code, 200)

    def test_manual_source_lifecycle(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            created = client.post("/v/api/ext/sources", json={
                "name": "示例", "api": "https://example.invalid/api.php/provide/vod"})
            self.assertEqual(created.status_code, 200)
            sid = created.json()["data"]["source"]["id"]
            self.assertFalse(created.json()["data"]["source"]["enabled"])
            enabled = client.patch(f"/v/api/ext/sources/{sid}", json={"enabled": True})
            self.assertTrue(enabled.json()["data"]["source"]["enabled"])
            removed = client.delete(f"/v/api/ext/sources/{sid}")
            self.assertEqual(removed.status_code, 200)

    def test_auto_update_defaults_off(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            cfg = client.get("/v/api/ext/bootstrap").json()["data"]["config"]
            self.assertFalse(cfg["auto_update"])
            self.assertEqual(cfg["update_interval_hours"], 24)
            self.assertEqual(cfg["subscription_url"], "")

    def test_full_fetch_enables_all_sources(self):
        payload = json.dumps({"api_site": {
            "a.invalid": {"name": "示例A", "api": "https://a.invalid/api.php/provide/vod"},
            "b.invalid": {"name": "示例B", "api": "https://b.invalid/api.php/provide/vod"},
            "c.invalid": {"name": "🔞跳过", "api": "https://c.invalid/api.php/provide/vod"},
        }})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            result = client.post("/v/api/ext/subscriptions/fetch",
                                 json={"text": payload}).json()["data"]
            self.assertEqual(result["added"], 3)
            self.assertEqual(result["adult"], 1)
            self.assertEqual(result["skipped"], 0)
            boot = client.get("/v/api/ext/bootstrap").json()["data"]
            self.assertEqual(boot["sources_total"], 3)
            self.assertEqual(boot["sources_enabled"], 2)
            marked = [s for s in boot["sources"] if s.get("adult")]
            self.assertEqual(len(marked), 1)
            self.assertFalse(marked[0]["enabled"])
            self.assertTrue(boot["config"]["config_text"])
            self.assertGreater(boot["last_update"], 0)
            config.save({**config.get(), "sources": [], "config_text": "",
                         "subscription_url": "", "last_update": 0})

    def test_second_fetch_replaces_subscription_sources(self):
        first = json.dumps({"api_site": {
            "a.invalid": {"name": "A", "api": "https://a.invalid/api.php/provide/vod"}}})
        second = json.dumps({"api_site": {
            "b.invalid": {"name": "B", "api": "https://b.invalid/api.php/provide/vod"}}})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            client.post("/v/api/ext/subscriptions/fetch", json={"text": first})
            client.post("/v/api/ext/sources", json={
                "name": "手工", "api": "https://m.invalid/api.php/provide/vod"})
            client.post("/v/api/ext/subscriptions/fetch", json={"text": second})
            apis = {s["api"] for s in
                    client.get("/v/api/ext/bootstrap").json()["data"]["sources"]}
            self.assertIn("https://b.invalid/api.php/provide/vod", apis)
            self.assertIn("https://m.invalid/api.php/provide/vod", apis)
            self.assertNotIn("https://a.invalid/api.php/provide/vod", apis)
            config.save({**config.get(), "sources": [], "config_text": "",
                         "subscription_url": "", "last_update": 0})

    def test_subscription_settings_saved(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            resp = client.patch("/v/api/ext/subscriptions/settings", json={
                "subscription_url": "https://example.invalid/sub.txt",
                "auto_update": True, "update_interval_hours": 6})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["data"]["auto_update"])
            self.assertEqual(resp.json()["data"]["update_interval_hours"], 6)
            client.patch("/v/api/ext/subscriptions/settings", json={
                "subscription_url": "", "auto_update": False,
                "update_interval_hours": 24})


    def test_search_merges_same_title_across_sources(self):
        import cms
        import cms as cms_mod
        import config as cfg_mod
        import fake

        def make(provider, name, line):
            return cms_mod.OnlineTitle(
                provider=provider, provider_name=name, vod_id="1",
                title="聚合剧", year="2025", type_name="电视剧",
                lines={line: [
                    cms_mod.Episode("第1集", f"https://{provider}.invalid/1.m3u8"),
                    cms_mod.Episode("第2集", f"https://{provider}.invalid/2.m3u8")]})

        titles = [make("p_a", "本地", "lz"), make("p_b", "网络", "tp")]
        cfg = {**cfg_mod.DEFAULTS, "merge_sources": True, "online_first": True,
               "sources": [{"id": "p_a", "name": "本地", "enabled": True,
                            "api": "https://a.invalid/api.php/provide/vod"}]}

        async def fake_search(*_args, **_kwargs):
            return titles

        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            with patch.object(config, "get", return_value=cfg), \
                    patch.object(config, "enabled_sources",
                                 return_value=cfg["sources"]), \
                    patch.object(cms, "search_all", new=fake_search):
                merged = client.get(
                    "/v/api/v1/search/list?q=聚合剧").json()["data"]
                merged = [c for c in merged
                          if str(c.get("title", "")).startswith("【在线】聚合剧")]
                self.assertEqual(len(merged), 1)
                brief = appmod.app.state.store.get(merged[0]["guid"])
                self.assertEqual(brief["line_names"], ["lz", "网络：tp"])
                labels = [q["resolution"] for q in fake.qualities(brief)]
                self.assertEqual(labels, ["本地：lz", "网络：tp"])

                cfg["merge_sources"] = False
                split = client.get(
                    "/v/api/v1/search/list?q=聚合剧").json()["data"]
                split = [c for c in split
                         if str(c.get("title", "")).startswith("【在线】聚合剧")]
                self.assertEqual(len(split), 2)

    def test_poster_routes_all_prefixes(self):
        from fastapi.responses import Response
        token = hls.encode_url("https://img.invalid/p.jpg")

        async def fake_poster(*_args, **_kwargs):
            return Response(b"PNG", media_type="image/png")

        with TestClient(appmod.app) as client:
            with patch.object(hls, "ensure_public_target", new=lambda u: u), \
                    patch.object(hls, "proxy_poster", new=fake_poster):
                for path in (f"/online/{token}.jpg",
                             f"/v/api/v1/sys/img/online/{token}.jpg",
                             f"/v/api/v1/sys/img//online/{token}.jpg",
                             f"/v/media/online/poster/{token}.jpg"):
                    self.assertEqual(client.get(path).status_code, 200, path)


    def test_main_m3u8_backfills_stream_meta(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="88", title="回填测试", type_name="电影",
            lines={"m3u8": [cms_mod.Episode("正片", "https://a.invalid/1.m3u8")]})
        master = ("#EXTM3U\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1280x720\nv.m3u8\n")
        meta = hls.build_playlist_meta(master)

        async def fake_fetch(*_args, **_kwargs):
            return "#EXTM3U\n#EXTINF:6.0,\nseg1.ts\n", "https://a.invalid/v.m3u8", meta

        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            mg = fake.media_guid(brief["guid"])
            with patch.object(hls, "ensure_public_target", new=lambda u: u), \
                    patch.object(hls, "fetch_playlist", new=fake_fetch):
                resp = client.get(f"/v/media/online/{mg}/main.m3u8?line=0&index=0")
            self.assertEqual(resp.status_code, 200)
            sl = client.get(f"/v/api/v1/stream/list/{brief['guid']}").json()["data"]
            video = sl["video_streams"][0]
            self.assertEqual(video["resolution_type"], "720p")
            self.assertEqual(video["width"], 1280)
            self.assertEqual(video["height"], 720)
            self.assertEqual(video["bps"], 5000000)


    def test_ordered_lines_skips_bad(self):
        brief = {"line_names": ["a", "b", "c"]}
        self.assertEqual(appmod._ordered_lines(brief, 0, set()), [0, 1, 2])
        self.assertEqual(appmod._ordered_lines(brief, 1, {1}), [2, 0])
        self.assertEqual(appmod._ordered_lines(brief, 0, {0, 1}), [2])
        self.assertEqual(appmod._ordered_lines(brief, 0, {0, 1, 2}), [0])
        self.assertEqual(appmod._ordered_lines({}, 0, set()), [])

    def test_main_m3u8_failover(self):
        import cms as cms_mod
        import config
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="99", title="换线测试", type_name="电影",
            lines={"m3u8": [cms_mod.Episode("正片", "https://a.invalid/1.m3u8")],
                   "备用": [cms_mod.Episode("正片", "https://b.invalid/1.m3u8")]})
        good = ("#EXTM3U\n#EXTINF:1.0,\ns.ts\n",
                "https://b.invalid/1.m3u8",
                {"width": 0, "height": 0, "bandwidth": 0})
        mock = AsyncMock(side_effect=[Exception("boom"), good])
        cfg = config.get()
        cfg["auto_best_source"] = False
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            guid = brief["guid"]
            mg = fake.media_guid(guid)
            with patch.object(config, "get", return_value=cfg), \
                    patch.object(hls, "ensure_public_target", new=lambda u: u), \
                    patch.object(hls, "fetch_playlist", new=mock):
                resp = client.get(f"/v/media/online/{mg}/main.m3u8?line=0&index=0")
            self.assertEqual(resp.status_code, 200)
            self.assertIn((guid, 0), appmod._BAD_LINES)
        appmod._BAD_LINES.clear()

    def test_main_m3u8_all_lines_fail(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="100", title="全挂测试", type_name="电影",
            lines={"m3u8": [cms_mod.Episode("正片", "https://a.invalid/1.m3u8")],
                   "备用": [cms_mod.Episode("正片", "https://b.invalid/1.m3u8")]})
        mock = AsyncMock(side_effect=[Exception("x"), Exception("y"),
                                       Exception("z")])
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            mg = fake.media_guid(brief["guid"])
            with patch.object(hls, "ensure_public_target", new=lambda u: u), \
                    patch.object(hls, "fetch_playlist", new=mock):
                resp = client.get(f"/v/media/online/{mg}/main.m3u8?line=0&index=0")
            self.assertEqual(resp.status_code, 502)
        appmod._BAD_LINES.clear()

    def test_play_play_avoids_bad_line(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="101", title="避坏测试", type_name="电影",
            lines={"m3u8": [cms_mod.Episode("正片", "https://a.invalid/1.m3u8")],
                   "备用": [cms_mod.Episode("正片", "https://b.invalid/1.mp4")]})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            guid = brief["guid"]
            mg = fake.media_guid(guid)
            appmod._mark_bad_line(guid, 0)
            resp = client.post(f"/v/api/v1/play/play", json={"media_guid": mg})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("line=1", resp.json()["data"]["play_link"])
        appmod._BAD_LINES.clear()


    def test_main_m3u8_race_picks_success(self):
        import cms as cms_mod
        import fake
        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="102", title="竞速测试", type_name="电影",
            lines={"m3u8": [cms_mod.Episode("正片", "https://a.invalid/1.m3u8")],
                   "备用": [cms_mod.Episode("正片", "https://b.invalid/1.m3u8")]})
        ok = ("#EXTM3U\n#EXTINF:1.0,\ns.ts\n",
              "https://b.invalid/1.m3u8",
              {"width": 0, "height": 0, "bandwidth": 0})
        mock = AsyncMock(side_effect=[Exception("boom"), ok])
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            mg = fake.media_guid(brief["guid"])
            with patch.object(hls, "ensure_public_target", new=lambda u: u), \
                    patch.object(hls, "fetch_playlist", new=mock):
                resp = client.get(
                    f"/v/media/online/{mg}/main.m3u8?line=0&index=0&auto=1")
            self.assertEqual(resp.status_code, 200)
        appmod._BAD_LINES.clear()


class HlsTest(unittest.TestCase):
    def test_fetch_resolves_share_page_to_m3u8(self):
        def handler(request):
            url = str(request.url)
            if url.endswith(".m3u8"):
                return httpx.Response(
                    200, text="#EXTM3U\n#EXTINF:6.0,\nseg.ts\n")
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<script>var u="https:\\/\\/cdn.invalid\\/p\\/index.m3u8";'
                     "</script>")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(hls, "ensure_public_target", new=lambda u: u):
            text, base, _meta = asyncio.run(
                hls.fetch_playlist(client, "https://play.invalid/play/abc"))
        self.assertIn("#EXTINF", text)
        self.assertIn("index.m3u8", base)

    def test_fetch_rejects_page_without_m3u8(self):
        def handler(_request):
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text="<html>403</html>")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(hls, "ensure_public_target", new=lambda u: u):
            with self.assertRaises(ValueError):
                asyncio.run(hls.fetch_playlist(
                    client, "https://play.invalid/play/abc"))

    def test_rewrite_relative_and_key(self):
        text = ("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.key\"\n"
                "#EXTINF:6.0,\nseg1.ts\n#EXTINF:6.0,\nhttps://cdn.invalid/seg2.ts\n")
        out = hls.rewrite_playlist(text, "https://cdn.invalid/live/index.m3u8",
                                   "/v/media/online/seg")
        self.assertIn("#EXT-X-KEY", out)
        self.assertIn("URI=\"/v/media/online/seg?u=", out)
        self.assertNotIn("seg1.ts", out)

    def test_url_token_roundtrip(self):
        url = "https://cdn.invalid/a/b.ts?token=x"
        self.assertEqual(hls.decode_url(hls.encode_url(url)), url)

    def test_ensure_public_target_allows_intranet_by_default(self):
        for url in ("http://127.0.0.1/x.ts", "http://localhost/x.ts",
                    "http://192.168.1.10/x.ts"):
            self.assertEqual(hls.ensure_public_target(url), url)

    def test_ensure_public_target_can_block_intranet(self):
        blocked = {**config.DEFAULTS, "allow_intranet": False}
        with patch.object(config, "get", return_value=blocked):
            for url in ("http://127.0.0.1/x.ts", "http://localhost/x.ts",
                        "http://192.168.1.10/x.ts"):
                with self.assertRaises(ValueError):
                    hls.ensure_public_target(url)

    def test_ensure_public_target_always_blocks_metadata(self):
        for url in ("http://169.254.169.254/latest/meta-data",
                    "http://0.0.0.0/x.ts", "file:///etc/passwd"):
            with self.assertRaises(ValueError):
                hls.ensure_public_target(url)

    def test_ensure_public_target_allows_public(self):
        url = hls.ensure_public_target("https://example.com/a.ts")
        self.assertEqual(url, "https://example.com/a.ts")

    def test_build_playlist_meta(self):
        master = ("#EXTM3U\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080\nv.m3u8\n")
        child = "#EXTM3U\n#EXTINF:4.0,\na.ts\n#EXTINF:2.0,\nb.ts\n#EXT-X-ENDLIST\n"
        meta = hls.build_playlist_meta(master, hls.build_playlist_meta(child))
        self.assertEqual(meta["width"], 1920)
        self.assertEqual(meta["height"], 1080)
        self.assertEqual(meta["bandwidth"], 8000000)
        self.assertEqual(meta["variant_count"], 1)
        self.assertEqual(meta["duration"], 6.0)
        self.assertTrue(meta["has_endlist"])
        media_meta = hls.build_playlist_meta(child)
        self.assertEqual(media_meta["width"], 0)
        self.assertEqual(media_meta["duration"], 6.0)

    def test_playlist_meta_cache(self):
        import time
        key = ("g", 0, 1)
        hls.remember_playlist_meta(key, {"width": 640, "height": 360})
        self.assertEqual(hls.get_playlist_meta(key)["height"], 360)
        expired = time.monotonic() - hls._META_TTL - 1
        hls._META[key] = (expired, {"width": 1, "height": 1})
        self.assertIsNone(hls.get_playlist_meta(key))

    def test_stream_inf_master_selection(self):
        master = ("#EXTM3U\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1280x720\nv720.m3u8\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080\nv1080.m3u8\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=640x360\nv360.m3u8\n")
        self.assertTrue(hls.is_master(master))
        self.assertEqual(hls.pick_best_variant(master), "v1080.m3u8")
        out = hls.rewrite_playlist(master, "https://cdn.invalid/live/master.m3u8",
                                   "/seg", "/variant", True)
        self.assertIn("/variant?u=", out)
        self.assertNotIn("/seg?u=", out)
        media = hls.rewrite_playlist("#EXTM3U\n#EXTINF:6.0,\nseg1.ts\n", "u", "/seg")
        self.assertIn("/seg?u=", media)
        self.assertNotIn("/variant?u=", media)


if __name__ == "__main__":
    unittest.main()
