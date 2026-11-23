import _bootstrap
import asyncio
import unittest
from unittest.mock import patch

import httpx
import cms
import fake
import hls
import hongguo
import upstream
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app as appmod


def _title(ext="mp4", url=None):
    link = url or (f"https://example.com/movie.{ext}")
    return cms.OnlineTitle(
        provider="s_1", vod_id="901", title="原画直链测试", type_name="电影",
        lines={"线路一": [cms.Episode("正片", link)]})


class MediaRangeTest(unittest.TestCase):
    def _install(self, handler):
        appmod.app.state.src = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))

    def _run(self, title, action):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([title])[0]
            return action(client, brief)

    def test_direct_mp4_range_proxy(self):
        seen = {}

        def handler(request):
            seen["range"] = request.headers.get("range")
            seen["url"] = str(request.url)
            return httpx.Response(
                206, headers={"content-range": "bytes 0-99/4000",
                              "content-type": "video/mp4",
                              "accept-ranges": "bytes"},
                content=b"\x11" * 100)

        def action(client, brief):
            mg = fake.media_guid(brief["guid"])
            return client.get(f"/v/api/v1/media/range/{mg}",
                              headers={"Range": "bytes=0-99"})
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            brief = appmod.app.state.store.remember([_title("mp4")])[0]
            self._install(handler)
            mg = fake.media_guid(brief["guid"])
            resp = client.get(f"/v/api/v1/media/range/{mg}",
                              headers={"Range": "bytes=0-99"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(seen["range"], "bytes=0-99")
        self.assertIn("movie.mp4", seen["url"])
        self.assertEqual(resp.headers["content-range"], "bytes 0-99/4000")
        self.assertEqual(len(resp.content), 100)

    def test_hls_redirects_to_playlist(self):
        def action(client, brief):
            self._install(lambda request: httpx.Response(500))
            mg = fake.media_guid(brief["guid"])
            return client.get(f"/v/api/v1/media/range/{mg}",
                              follow_redirects=False)
        resp = self._run(_title("m3u8"), action)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/v/media/online/", resp.headers["location"])
        self.assertIn("main.m3u8", resp.headers["location"])
        self.assertIn("line=0", resp.headers["location"])
        self.assertIn("auto=1", resp.headers["location"])

    def test_hls_redirect_honors_play_link_line(self):
        title = cms.OnlineTitle(
            provider="s_1", vod_id="903", title="原生切线", type_name="电影",
            lines={"a": [cms.Episode("正片", "https://example.com/a.m3u8")],
                   "b": [cms.Episode("正片", "https://example.com/b.m3u8")]})

        def action(client, brief):
            mg = fake.media_guid(brief["guid"])
            link = f"/v/media/online/{mg}/preset.m3u8?line=1&index=0"
            return client.get(f"/v/api/v1/media/range/{mg}",
                              headers={"Play-Link": link},
                              follow_redirects=False)
        resp = self._run(title, action)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("line=1", resp.headers["location"])
        self.assertNotIn("auto=1", resp.headers["location"])

    def test_new_ios_gets_playlist_without_redirect(self):
        playlist = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
                    "#EXT-X-MEDIA-SEQUENCE:0\n"
                    "#EXTINF:6.000,\nseg1.ts\n#EXTINF:6.000,\nseg2.ts\n"
                    "#EXT-X-ENDLIST\n")

        def handler(request):
            if "movie.m3u8" in str(request.url):
                return httpx.Response(
                    200, headers={"content-type": "application/vnd.apple.mpegurl"},
                    content=playlist.encode())
            return httpx.Response(200, content=b"\x00")

        def action(client, brief):
            self._install(handler)
            mg = fake.media_guid(brief["guid"])
            ua = ("1.13.0 (com.trim.media.ios; build:1130000; "
                  "iOS 27.2) Flutter/3.11.5")
            first = client.get(f"/v/api/v1/media/range/{mg}",
                               headers={"User-Agent": ua},
                               follow_redirects=False)
            second = client.get(first.headers["location"],
                                headers={"User-Agent": ua},
                                follow_redirects=False)
            return first, second
        first, second = self._run(_title("m3u8"), action)
        # 统一入口：302 到 preset(mr)，由它直接返回媒体清单。
        self.assertEqual(first.status_code, 302)
        self.assertIn("/preset.m3u8", first.headers["location"])
        self.assertIn("from=mr", first.headers["location"])
        self.assertEqual(second.status_code, 200)
        self.assertIn("mpegurl", second.headers["content-type"])
        self.assertIn("#EXTM3U", second.text)
        # 快源：预热结果直接进缓存分支下发，短 id 分片地址可用（实测 206）。
        self.assertRegex(second.text, r"/v/media/online/seg/[0-9a-f]{16}\.ts")
        self.assertNotIn("?u=", second.text)

    def test_ios_slow_playlist_waits_for_real_playlist(self):
        import asyncio

        async def handler(request):
            await asyncio.sleep(3.0)
            return httpx.Response(
                200, headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXTINF:6.0,\ns.ts\n#EXT-X-ENDLIST\n")

        def action(client, brief):
            self._install(handler)
            mg = fake.media_guid(brief["guid"])
            return client.get(
                f"/v/api/v1/media/range/{mg}",
                headers={"User-Agent": "1.13.0 (com.trim.media.ios; "
                                       "build:1130000; iOS 27.2) Flutter/3.11.5"},
                follow_redirects=True)
        resp = self._run(_title("m3u8", "https://example.com/slow.m3u8"),
                         action)
        # 慢源：不下发占位假清单（1.13 的 fncache 会把它缓存成播放会话导致
        # 整部片只转圈），多等几秒拿真实点播清单。
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("x-fnmedia-placeholder", resp.headers)
        self.assertNotIn("_placeholder/placeholder.ts", resp.text)
        self.assertTrue(resp.text.startswith("#EXTM3U"))
        self.assertIn("/v/media/online/seg?u=", resp.text)

    def test_play_link_stays_relative(self):
        ua = ("1.13.0 (com.trim.media.ios; build:1130000; "
              "iOS 27.2) Flutter/3.11.5")

        def action(client, brief):
            mg = fake.media_guid(brief["guid"])
            ios = client.post("/v/api/v1/play/play", json={"media_guid": mg},
                              headers={"User-Agent": ua}).json()["data"]
            web = client.post("/v/api/v1/play/play",
                              json={"media_guid": mg}).json()["data"]
            return ios, web
        ios, web = self._run(_title("m3u8"), action)
        # 两端都用相对地址：App 按自己配置的服务器地址（含端口）解析，
        # 绝对地址会因反代 Host 不带端口而丢掉 5666。
        self.assertTrue(ios["play_link"].startswith("/v/media/online/"))
        self.assertTrue(web["play_link"].startswith("/v/media/online/"))

    def test_official_hash_passthrough(self):
        async def fake_forward(req, client_):
            return JSONResponse({"data": "official"}, status_code=200)

        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            with patch.object(upstream, "forward", fake_forward):
                resp = client.get(
                    "/v/api/v1/media/range/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"], "official")

    def test_episode_guid_resolves(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, headers={"content-type": "video/mp4"},
                                  content=b"\x22" * 32)

        def action(client, brief):
            self._install(handler)
            eg = fake.episode_guid(brief["guid"], 1)
            return client.get(f"/v/api/v1/media/range/{eg}")
        title = cms.OnlineTitle(
            provider="s_1", vod_id="902", title="剧集直链测试",
            type_name="电视剧",
            lines={"线路一": [cms.Episode("第1集", "https://example.com/e1.mp4"),
                             cms.Episode("第2集", "https://example.com/e2.mp4")]})
        resp = self._run(title, action)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("e2.mp4", seen["url"])

    def test_blocked_line_404(self):
        def action(client, brief):
            self._install(lambda request: httpx.Response(500))
            mg = fake.media_guid(brief["guid"])
            with patch.object(hls, "ensure_public_target",
                              side_effect=ValueError("blocked")):
                return client.get(f"/v/api/v1/media/range/{mg}")
        resp = self._run(_title("mp4"), action)
        self.assertEqual(resp.status_code, 404)

    def test_slice_playlist_to_resume_offset(self):
        """iOS 续播：清单裁到断点分片，MEDIA-SEQUENCE 同步上调。"""
        playlist = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
                    "#EXT-X-MEDIA-SEQUENCE:100\n"
                    + "".join(f"#EXTINF:6.000,\nseg{i}.ts\n" for i in range(4))
                    + "#EXT-X-ENDLIST\n")
        out, dropped = hls.slice_from_offset(playlist, 13)
        self.assertEqual(dropped, 2)
        self.assertTrue(out.startswith("#EXTM3U\n"), "EXTM3U 魔术行不能丢")
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:102", out)
        self.assertNotIn("seg0.ts", out)
        self.assertNotIn("seg1.ts", out)
        self.assertIn("seg2.ts", out)
        self.assertIn("#EXT-X-ENDLIST", out)
        self.assertNotIn("#EXT-X-START", out)

    def test_slice_playlist_noop_cases(self):
        """起点无效（0 / 超出片长）时原样返回，起播路径不受影响。"""
        playlist = ("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n"
                    "#EXTINF:6.000,\nseg0.ts\n#EXT-X-ENDLIST\n")
        self.assertEqual(hls.slice_from_offset(playlist, 0), (playlist, 0))
        self.assertEqual(hls.slice_from_offset(playlist, 600), (playlist, 0))
        # 直播窗口（无 ENDLIST）不裁剪
        live = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n#EXTINF:6.0,\ns.ts\n"
        self.assertEqual(hls.slice_from_offset(live, 60), (live, 0))

    def test_slice_playlist_keeps_trailing_tags_with_last_segment(self):
        """收尾标签（ENDLIST）与中间标签（DISCONTINUITY）跟随分片保留。"""
        playlist = ("#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                    "#EXTINF:6.000,\na.ts\n#EXT-X-DISCONTINUITY\n"
                    "#EXTINF:6.000,\nb.ts\n#EXT-X-ENDLIST\n")
        out, dropped = hls.slice_from_offset(playlist, 8)
        self.assertEqual(dropped, 1)
        self.assertIn("#EXT-X-DISCONTINUITY", out)
        self.assertIn("b.ts", out)
        self.assertNotIn("a.ts\n#EXT-X-DISCONTINUITY", out)


class HongguoFallbackTest(unittest.TestCase):
    """红果高清档被限流时，媒体代理换 compat 重取一次，而不是回 503。"""

    EP = "https://hg.example.com/api/stream/7241?q=auto&at=t0k"

    def _title(self):
        return cms.OnlineTitle(
            provider="hg_x", vod_id="900", title="红果兜底测试", type_name="短剧",
            lines={"红果": [cms.Episode("第1集", self.EP, direct=True),
                          cms.Episode("第2集", self.EP.replace("7241", "7242"),
                                      direct=True)]})

    def test_compat_url_rewrites_quality_only(self):
        alt = hongguo.compat_url(self.EP)
        self.assertIn("q=compat", alt)
        self.assertIn("at=t0k", alt)
        self.assertIn("/api/stream/7241", alt)
        self.assertEqual(hongguo.compat_url(alt), "")
        self.assertEqual(hongguo.compat_url("https://x.com/y.m3u8"), "")

    def test_auto_503_retries_with_compat(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if "q=auto" in str(request.url):
                return httpx.Response(503, json={"error": "同时播放数已达上限",
                                                 "code": "BUSY"})
            return httpx.Response(200, content=b"\x22" * 64,
                                  headers={"content-type": "video/mp4",
                                           "x-stream-quality": "compat",
                                           "x-stream-codec": "h264"})

        with patch.object(hls, "ensure_public_target", side_effect=lambda u: u):
            with TestClient(appmod.app) as client:
                client.get("/v/online-admin")
                appmod.app.state.src = httpx.AsyncClient(
                    transport=httpx.MockTransport(handler))
                brief = appmod.app.state.store.remember([self._title()])[0]
                mg = fake.ep_media_guid(fake.episode_guid(brief["guid"], 1))
                resp = client.get(f"/v/api/v1/media/range/{mg}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(seen), 2)
        self.assertIn("q=auto", seen[0])
        self.assertIn("q=compat", seen[1])
        self.assertIn("7242", seen[1])

    def test_client_error_is_not_retried(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(404, json={"error": "剧集不存在"})

        with patch.object(hls, "ensure_public_target", side_effect=lambda u: u):
            with TestClient(appmod.app) as client:
                client.get("/v/online-admin")
                appmod.app.state.src = httpx.AsyncClient(
                    transport=httpx.MockTransport(handler))
                brief = appmod.app.state.store.remember([self._title()])[0]
                mg = fake.ep_media_guid(fake.episode_guid(brief["guid"], 0))
                resp = client.get(f"/v/api/v1/media/range/{mg}")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(seen), 1)

    def test_prewarm_current_and_next_episode_once(self):
        """起播预热只打当前集与下一集各一次，重复调用不再打上游。"""
        seen = []

        def handler(request):
            seen.append(request.headers.get("range", ""))
            seen.append(str(request.url))
            return httpx.Response(206, content=b"\x00" * 16,
                                  headers={"content-range": "bytes 0-15/100",
                                           "content-type": "video/mp4"})

        appmod._HG_WARM.clear()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        first = "https://hg.example.com/api/stream/1?q=auto"
        second = "https://hg.example.com/api/stream/2?q=auto"

        async def run():
            appmod._prewarm_hongguo(client, first, second)
            await asyncio.sleep(0.05)
            appmod._prewarm_hongguo(client, first, second)
            await asyncio.sleep(0.05)
            await client.aclose()

        asyncio.run(run())
        urls = [item for item in seen if item.startswith("http")]
        self.assertEqual(len(urls), 2)
        self.assertTrue(any("/api/stream/2" in u for u in urls))
        self.assertIn("bytes=0-1023", seen)
        appmod._HG_WARM.clear()


if __name__ == "__main__":
    unittest.main()
