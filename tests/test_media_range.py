import _bootstrap
import unittest
from unittest.mock import patch

import httpx
import cms
import fake
import hls
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
        mg = None
        self.assertIn("/v/media/online/", resp.headers["location"])
        self.assertIn("main.m3u8", resp.headers["location"])
        self.assertIn("line=0", resp.headers["location"])

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


if __name__ == "__main__":
    unittest.main()
