import _bootstrap  # noqa: F401
import asyncio
import re
import unittest
from unittest.mock import patch

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

REAL_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:8.0,
https://vod2.maowushi.com/20231212/5xBGixD0/hls/pn5cHP9m.ts
#EXTINF:8.0,
https://vod2.maowushi.com/20231212/5xBGixD0/hls/qR7tVs2k.ts
#EXTINF:8.0,
https://vod2.maowushi.com/20231212/5xBGixD0/hls/zM3bN8xw.ts
#EXT-X-ENDLIST
"""

LONG_PLAYLIST = PLAYLIST.replace(
    "#EXT-X-ENDLIST",
    "#EXTINF:10.0,\nhttps://cdn.example.com/vod/part3.ts\n"
    "#EXTINF:10.0,\nhttps://cdn.example.com/vod/part4.ts\n"
    "#EXTINF:10.0,\nhttps://cdn.example.com/vod/part5.ts\n"
    "#EXT-X-ENDLIST")

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
                "adfilter_keywords": [], "adfilter_head_skip": 0.0,
                "adfilter_head_guard": True, "adfilter_breaks": True})
    cfg.update(overrides)
    config.save(cfg)


def build_playlist(spec):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    idx = 0
    for number, (count, duration, prefix, host) in enumerate(spec):
        if number:
            lines.append("#EXT-X-DISCONTINUITY")
        for _ in range(count):
            lines.append(f"#EXTINF:{duration:.1f},")
            lines.append(f"https://{host}/{prefix}/s{idx}.ts")
            idx += 1
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


CONTENT = (30, 10.0, "vod", "cdn.example.com")
AD_BREAK = (3, 5.0, "ad", "cdn.example.com")


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
        reset(adfilter_enabled=True, adfilter_head_skip=20.0,
              adfilter_head_guard=False)
        text, stats = hls.strip_ads(LONG_PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 2)
        self.assertEqual(stats["by_head"], 2)
        self.assertNotIn("intro.ts", text)
        self.assertNotIn("part1.ts", text)
        self.assertIn("part2.ts", text)
        self.assertAlmostEqual(stats["kept_duration"], 40.0)

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


    def test_head_guard_keeps_same_stream_segments(self):
        reset(adfilter_enabled=True, adfilter_head_skip=20.0)
        text, stats = hls.strip_ads(PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["guard_kept"], 1)
        self.assertIn("suspect", stats["guard"])
        self.assertNotIn("intro.ts", text)
        self.assertIn("vod/part1.ts", text)

    def test_head_guard_allows_different_directory(self):
        playlist = LONG_PLAYLIST.replace(
            "https://cdn.example.com/vod/part1.ts",
            "https://cdn.example.com/ad/part1.ts")
        reset(adfilter_enabled=True, adfilter_head_skip=20.0)
        _, stats = hls.strip_ads(playlist, BASE)
        self.assertEqual(stats["removed"], 2)
        self.assertEqual(stats["guard_kept"], 0)

    def test_live_playlist_ignores_head_skip(self):
        live = PLAYLIST.replace("#EXT-X-ENDLIST\n", "")
        reset(adfilter_enabled=True, adfilter_head_skip=20.0,
              adfilter_head_guard=False)
        text, stats = hls.strip_ads(live, BASE)
        self.assertEqual(stats["removed"], 0)
        self.assertIn("live playlist", stats["guard"])
        self.assertEqual(text, live)

    def test_bulk_removal_is_reverted(self):
        reset(adfilter_enabled=True, adfilter_keywords=["cdn.example"])
        text, stats = hls.strip_ads(PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(stats["guard"], "bulk removal reverted")
        self.assertEqual(text, PLAYLIST)
        self.assertAlmostEqual(stats["kept_duration"], 30.0)

    def test_rewrite_logs_cut_duration_and_records_preview(self):
        reset(adfilter_enabled=True, adfilter_keywords=["/ad/"])
        hls._AD_PREVIEWS.clear()
        with patch.object(hls.runtime, "log") as logged:
            out = hls.rewrite_playlist(PLAYLIST, BASE, SEG_PREFIX)
        self.assertNotIn(hls._AD_PLACEHOLDER, out)
        message = logged.call_args[0][0] if logged.call_args else ""
        self.assertIn("removed=1", message)
        self.assertIn("cut=10s", message)
        self.assertIn("total=30s", message)
        self.assertIn("intro.ts", message)
        previews = hls.recent_ad_previews(1)
        self.assertEqual(len(previews), 1)
        item = previews[0]
        self.assertEqual(item["removed"], 1)
        self.assertEqual(len(item["dropped"]), 1)
        self.assertIn("intro.ts", item["dropped"][0]["url"])
        self.assertEqual(item["dropped"][0]["reason"], "keyword")
        self.assertIn("#EXTM3U", item["raw_head"])
        self.assertIn("kept_duration", item)

    def test_real_world_head_only_misdetect_is_guarded(self):
        reset(adfilter_enabled=True, adfilter_head_skip=8.0)
        text, stats = hls.strip_ads(REAL_PLAYLIST, BASE)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(stats["guard_kept"], 1)
        self.assertIn("suspect", stats["guard"])
        self.assertEqual(text, REAL_PLAYLIST)
        self.assertIn("pn5cHP9m.ts", text)

    def test_preview_endpoint_requires_admin_token(self):
        with TestClient(appmod.app) as client:
            denied = client.get("/v/api/ext/adfilter/preview")
            self.assertEqual(denied.status_code, 401)
            client.get("/v/online-admin")
            allowed = client.get("/v/api/ext/adfilter/preview")
            self.assertEqual(allowed.status_code, 200)
            self.assertIsInstance(allowed.json()["data"]["previews"], list)


    def _smart_rules(self):
        reset(adfilter_enabled=True, adfilter_markers=False,
              adfilter_keywords=[], adfilter_head_skip=0.0)

    def test_discontinuity_break_group_is_removed(self):
        self._smart_rules()
        playlist = build_playlist([CONTENT, AD_BREAK, CONTENT])
        text, stats = hls.strip_ads(playlist, BASE)
        self.assertEqual(stats["removed"], 3)
        self.assertEqual(stats["by_break"], 3)
        self.assertEqual(len(stats["groups"]), 3)
        self.assertNotIn("/ad/", text)
        self.assertIn("/vod/", text)

    def test_single_group_playlist_is_exempt(self):
        self._smart_rules()
        playlist = build_playlist([CONTENT])
        text, stats = hls.strip_ads(playlist, BASE)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(text, playlist)

    def test_dense_discontinuity_playlist_is_exempt(self):
        self._smart_rules()
        spec = [CONTENT] + [(3, 5.0, f"ad{i}", "cdn.example.com")
                            for i in range(10)]
        _, stats = hls.strip_ads(build_playlist(spec), BASE)
        self.assertEqual(len(stats["groups"]), 11)
        self.assertEqual(stats["removed"], 0)

    def test_break_rule_can_be_disabled(self):
        reset(adfilter_enabled=True, adfilter_markers=False,
              adfilter_keywords=[], adfilter_head_skip=0.0,
              adfilter_breaks=False)
        _, stats = hls.strip_ads(
            build_playlist([CONTENT, AD_BREAK, CONTENT]), BASE)
        self.assertEqual(stats["removed"], 0)

    def test_break_groups_over_budget_are_reverted(self):
        self._smart_rules()
        spec = [CONTENT] + [(15, 8.0, f"ad{i}", "cdn.example.com")
                            for i in range(3)]
        playlist = build_playlist(spec)
        with patch.object(hls, "_AD_MAX_GROUP_RELATIVE", 2.0):
            text, stats = hls.strip_ads(playlist, BASE)
        self.assertEqual(stats["removed"], 0)
        self.assertIn("20%", stats["guard"])
        self.assertEqual(text, playlist)

    def test_duplicate_segment_is_removed(self):
        self._smart_rules()
        playlist = PLAYLIST.replace(
            "https://cdn.example.com/vod/part2.ts",
            "https://cdn.example.com/vod/part1.ts")
        _, stats = hls.strip_ads(playlist, BASE)
        self.assertEqual(stats["by_duplicate"], 1)
        self.assertEqual(stats["removed"], 1)

    def test_tiny_tracking_segment_is_removed(self):
        self._smart_rules()
        playlist = PLAYLIST.replace(
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:0.2,\n"
            "https://cdn.example.com/track.gif")
        _, stats = hls.strip_ads(playlist, BASE)
        self.assertEqual(stats["by_tiny"], 1)

    def test_minority_host_is_removed(self):
        self._smart_rules()
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10",
                 "#EXT-X-MEDIA-SEQUENCE:0"]
        for i in range(20):
            host = "ads.other.com" if i == 5 else "cdn.example.com"
            lines.append("#EXTINF:10.0,")
            lines.append(f"https://{host}/vod/s{i}.ts")
        lines.append("#EXT-X-ENDLIST")
        _, stats = hls.strip_ads("\n".join(lines) + "\n", BASE)
        self.assertEqual(stats["by_minority"], 1)

    def test_break_hits_are_exposed_in_preview(self):
        self._smart_rules()
        hls._AD_PREVIEWS.clear()
        playlist = build_playlist([CONTENT, AD_BREAK, CONTENT])
        hls.rewrite_playlist(playlist, BASE, SEG_PREFIX)
        item = hls.recent_ad_previews(1)[0]
        self.assertEqual(item["by_break"], 3)
        self.assertEqual(len(item["groups"]), 3)
        self.assertEqual(item["dropped"][0]["reason"], "break")


if __name__ == "__main__":
    unittest.main()
