import _bootstrap  # noqa: F401
import asyncio
import re
import unittest

import httpx
from fastapi.testclient import TestClient

import app as appmod
import config
import hls

BASE = "https://cdn.example.com/list.m3u8"
SEG_PREFIX = "/v/media/online/seg"

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:10.0,
https://cdn.example.com/ad/intro.ts
#EXTINF:10.0,
https://cdn.example.com/vod/part1.ts
#EXTINF:10.0,
https://cdn.example.com/vod/part2.ts
#EXT-X-ENDLIST
"""

AES_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example.com/key.key"
#EXTINF:10.0,
https://cdn.example.com/ad/intro.ts
#EXTINF:10.0,
https://cdn.example.com/vod/part1.ts
#EXT-X-ENDLIST
"""

CUE_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
https://cdn.example.com/vod/part1.ts
#EXT-X-CUE-OUT:DURATION=20
#EXTINF:10.0,
https://cdn.example.com/break/1.ts
#EXTINF:10.0,
https://cdn.example.com/break/2.ts
#EXT-X-CUE-IN
#EXTINF:10.0,
https://cdn.example.com/vod/part2.ts
#EXT-X-ENDLIST
"""

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080
https://cdn.example.com/ad/high.m3u8
"""


def reset(**overrides):
    cfg = config.get()
    cfg.update({"adfilter_enabled": False, "adfilter_markers": True,
                "adfilter_keywords": [], "adfilter_head_skip": 0.0})
    cfg.update(overrides)
    config.save(cfg)


class AdFilterTest(unittest.TestCase):
    def setUp(self):
        reset()

    def tearDown(self):
        reset()

    def test_disabled_returns_original_text(self):
        text, stats = hls.strip_ads(PLAYLIST, BASE)
        self.assertEqual(text, PLAYLIST)
        self.assertEqual(stats["removed"], 0)
        self.assertAlmostEqual(stats["kept_duration"], 30.0)

    def test_disabled_rewrite_keeps_every_segment(self):
        out = hls.rewrite_playlist(PLAYLIST, BASE, SEG_PREFIX)
        self.assertEqual(len(re.findall(r"\?u=", out)), 3)
        self.assertNotIn(hls._AD_PLACEHOLDER, out)

    def test_keyword_removes_segment_and_keeps_global_tags(self):
        reset(adfilter_enabled=True, adfilter_keywords=["/ad/"])
        text, stats = hls.strip_ads(PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["by_keyword"], 1)
        self.assertNotIn("intro.ts", text)
        self.assertIn("#EXTM3U", text)
        self.assertIn("#EXT-X-TARGETDURATION:10", text)
        self.assertIn("#EXT-X-ENDLIST", text)
        self.assertAlmostEqual(stats["kept_duration"], 20.0)
        out = hls.rewrite_playlist(PLAYLIST, BASE, SEG_PREFIX)
        self.assertEqual(len(re.findall(r"\?u=", out)), 2)
        self.assertNotIn(hls._AD_PLACEHOLDER, out)
        self.assertNotIn("__FNMEDIA_AD_SEGMENT__", out)

    def test_removed_segment_keeps_original_aes_sequence(self):
        reset(adfilter_enabled=True, adfilter_keywords=["/ad/"])
        out = hls.rewrite_playlist(AES_PLAYLIST, BASE, SEG_PREFIX,
                                   decrypt_keys=True)
        tokens = re.findall(r"\?u=(k[^\s]+)", out)
        self.assertEqual(len(tokens), 1)
        url, key_url, iv = hls.decode_seg(tokens[0])
        self.assertTrue(url.endswith("part1.ts"))
        self.assertTrue(key_url.endswith("key.key"))
        self.assertEqual(iv, f"{8:032x}")
        self.assertEqual(iv, "00000000000000000000000000000008")
        self.assertNotIn("intro.ts", out)

    def test_head_skip_removes_leading_segments(self):
        reset(adfilter_enabled=True, adfilter_head_skip=20.0)
        text, stats = hls.strip_ads(PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 2)
        self.assertEqual(stats["by_head"], 2)
        self.assertNotIn("intro.ts", text)
        self.assertNotIn("part1.ts", text)
        self.assertIn("part2.ts", text)
        self.assertAlmostEqual(stats["kept_duration"], 10.0)

    def test_cue_markers_remove_only_the_ad_break(self):
        reset(adfilter_enabled=True, adfilter_keywords=[])
        text, stats = hls.strip_ads(CUE_PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 2)
        self.assertEqual(stats["by_marker"], 2)
        self.assertIn("vod/part1.ts", text)
        self.assertIn("vod/part2.ts", text)
        self.assertNotIn("break/1.ts", text)

    def test_marker_rule_can_be_turned_off(self):
        reset(adfilter_enabled=True, adfilter_markers=False,
              adfilter_keywords=[])
        _, stats = hls.strip_ads(CUE_PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 0)

    def test_master_playlist_is_untouched(self):
        reset(adfilter_enabled=True, adfilter_keywords=["ad"])
        text, stats = hls.strip_ads(MASTER, BASE)
        self.assertEqual(text, MASTER)
        self.assertEqual(stats["removed"], 0)

    def test_config_patch_clamps_values(self):
        config.patch({"adfilter_enabled": True, "adfilter_head_skip": 999,
                      "adfilter_keywords": [" AD ", "", "/GG/"]})
        cfg = config.get()
        self.assertTrue(cfg["adfilter_enabled"])
        self.assertEqual(cfg["adfilter_head_skip"], 120.0)
        self.assertEqual(cfg["adfilter_keywords"], ["ad", "/gg/"])

    def test_admin_api_roundtrip(self):
        with TestClient(appmod.app) as client:
            client.get("/v/online-admin")
            resp = client.patch("/v/api/ext/config", json={
                "adfilter_enabled": True,
                "adfilter_markers": True,
                "adfilter_head_skip": 15,
                "adfilter_keywords": ["advert"],
            })
            self.assertEqual(resp.status_code, 200)
            cfg = client.get("/v/api/ext/bootstrap").json()["data"]["config"]
            self.assertTrue(cfg["adfilter_enabled"])
            self.assertEqual(cfg["adfilter_head_skip"], 15.0)
            self.assertEqual(cfg["adfilter_keywords"], ["advert"])
            self.assertNotIn("admin_token", cfg)

    def test_fetch_playlist_recomputes_duration(self):
        reset(adfilter_enabled=True, adfilter_keywords=["/ad/"])

        def handler(request):
            return httpx.Response(200, text=PLAYLIST)

        async def run():
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler))
            try:
                return await hls.fetch_playlist(client, BASE)
            finally:
                await client.aclose()

        _, _, meta = asyncio.run(run())
        self.assertAlmostEqual(meta["duration"], 20.0)


if __name__ == "__main__":
    unittest.main()
