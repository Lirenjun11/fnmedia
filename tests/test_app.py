import _bootstrap  
import asyncio
import json
import os
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
            video = data["video_streams"][0]
            self.assertEqual(video["resolution_type"], "720p")
            self.assertEqual(video["bps"], 5000000)
            self.assertEqual(len(data["qualities"]), 1)

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
                resp = client.get(f"/v/media/online/{mg}/main.m3u8?line=0&index=0")
            self.assertEqual(resp.status_code, 200)
        appmod._BAD_LINES.clear()


class HlsTest(unittest.TestCase):
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
