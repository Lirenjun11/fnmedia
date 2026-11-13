import _bootstrap  # noqa: F401
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.responses import Response
from fastapi.testclient import TestClient

import app as appmod
import config
import hls

SEG = "/v/media/online/seg"
BASE = "https://cdn.example.com/list.m3u8"
PUBLIC = "https://93.184.216.34/vod/a.ts"
PRIVATE = "https://192.168.1.10/vod/a.ts"

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
https://cdn.example.com/ad/intro.ts
#EXTINF:10.0,
https://cdn.example.com/vod/part1.ts
#EXTINF:10.0,
https://cdn.example.com/vod/part2.ts
#EXT-X-ENDLIST
"""


class SegRedirectTest(unittest.TestCase):
    def setUp(self):
        cfg = config.get()
        self._orig = {key: cfg.get(key) for key in (
            "seg_redirect", "seg_redirect_block_hosts", "acl_enabled",
            "allow_intranet", "adfilter_enabled", "adfilter_keywords",
            "adfilter_markers", "adfilter_breaks", "adfilter_strong",
            "adfilter_head_skip")}
        cfg.update({
            "seg_redirect": True,
            "seg_redirect_block_hosts": [],
            "acl_enabled": False,
            "allow_intranet": True,
            "adfilter_enabled": False,
            "adfilter_keywords": [],
            "adfilter_markers": False,
            "adfilter_breaks": False,
            "adfilter_strong": False,
            "adfilter_head_skip": 0.0,
        })
        config.save(cfg)
        hls._REDIRECT_PROBE.clear()
        hls._REDIRECT_SEEN.clear()

    def tearDown(self):
        hls._REDIRECT_PROBE.clear()
        hls._REDIRECT_SEEN.clear()
        cfg = config.get()
        cfg.update(self._orig)
        config.save(cfg)

    def token(self, url=PUBLIC, key_url=""):
        if key_url:
            return hls.encode_seg(url, key_url, "0" * 32)
        return hls.encode_url(url)

    def request(self, token, probe=(True, PUBLIC)):
        with patch("hls._probe_direct", new=AsyncMock(return_value=probe)), \
                patch("hls.fetch_key", new=AsyncMock(
                    side_effect=RuntimeError("skip"))), \
                patch("hls.fetch_media_bytes", new=AsyncMock(
                    side_effect=RuntimeError("skip"))), \
                patch("hls.proxy_media", new=AsyncMock(
                    return_value=Response(content=b"proxy", status_code=200))):
            with TestClient(appmod.app) as client:
                return client.get(f"{SEG}?u={token}", follow_redirects=False)

    def test_can_redirect_returns_302(self):
        resp = self.request(self.token())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], PUBLIC)
        self.assertEqual(resp.headers["cache-control"], "no-store")

    def test_probe_failed_falls_back_to_proxy(self):
        resp = self.request(self.token(), probe=(False, PUBLIC))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"proxy")

    def test_encrypted_segment_never_redirects(self):
        token = self.token(key_url="https://93.184.216.34/key.key")
        resp = self.request(token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"proxy")

    def test_private_target_never_redirects(self):
        resp = self.request(self.token(PRIVATE), probe=(True, PRIVATE))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"proxy")

    def test_blocked_host_never_redirects(self):
        cfg = config.get()
        cfg["seg_redirect_block_hosts"] = ["93.184.216.34"]
        config.save(cfg)
        resp = self.request(self.token())
        self.assertEqual(resp.status_code, 200)

    def test_switch_off_never_redirects(self):
        cfg = config.get()
        cfg["seg_redirect"] = False
        config.save(cfg)
        resp = self.request(self.token())
        self.assertEqual(resp.status_code, 200)

    def test_acl_on_never_redirects(self):
        cfg = config.get()
        cfg["acl_enabled"] = True
        config.save(cfg)
        resp = self.request(self.token())
        self.assertEqual(resp.status_code, 200)

    def test_retry_of_same_token_falls_back_to_proxy(self):
        token = self.token()
        with patch("hls._probe_direct", new=AsyncMock(
                return_value=(True, PUBLIC))), \
                patch("hls.proxy_media", new=AsyncMock(
                    return_value=Response(content=b"proxy", status_code=200))):
            with TestClient(appmod.app) as client:
                first = client.get(f"{SEG}?u={token}", follow_redirects=False)
                second = client.get(f"{SEG}?u={token}", follow_redirects=False)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, b"proxy")

    def test_probe_result_is_cached_per_host(self):
        with patch("hls._probe_direct", new=AsyncMock(
                return_value=(True, PUBLIC))) as probe, \
                patch("hls.proxy_media", new=AsyncMock(
                    return_value=Response(content=b"proxy", status_code=200))):
            with TestClient(appmod.app) as client:
                client.get(f"{SEG}?u={hls.encode_url(PUBLIC)}",
                           follow_redirects=False)
                other = PUBLIC.replace("a.ts", "b.ts")
                client.get(f"{SEG}?u={hls.encode_url(other)}",
                           follow_redirects=False)
        self.assertEqual(probe.await_count, 1)

    def test_adfilter_still_rewrites_playlist(self):
        cfg = config.get()
        cfg.update({"adfilter_enabled": True, "adfilter_keywords": ["/ad/"]})
        config.save(cfg)
        out = hls.rewrite_playlist(PLAYLIST, BASE, SEG, decrypt_keys=False)
        self.assertNotIn("intro.ts", out)
        self.assertIn(f"{SEG}?u=", out)
        self.assertNotIn(hls._AD_PLACEHOLDER, out)


if __name__ == "__main__":
    unittest.main()
