import _bootstrap  
import json
import unittest

import cms
import config
import fake


class ConfigTest(unittest.TestCase):
    def test_zero_sources_by_default(self):
        cfg = config.get()
        self.assertEqual(cfg["sources"], [])
        self.assertEqual(cfg["subscriptions"], [])
        self.assertEqual(config.enabled_sources(), [])
        self.assertTrue(cfg["admin_token"])

    def test_patch_and_limit(self):
        config.patch({"search_timeout": 99, "enabled_limit": 2})
        self.assertLessEqual(config.get()["search_timeout"], 10)
        sources = [{"id": f"s{i}", "name": str(i),
                    "api": "https://example.invalid/api.php/provide/vod",
                    "enabled": True} for i in range(6)]
        config.save({**config.get(), "sources": sources})
        self.assertEqual(len(config.enabled_sources()), 2)
        config.save({**config.get(), "sources": []})


class CmsTest(unittest.TestCase):
    def test_parse_play(self):
        lines = cms.parse_play("m3u8$$$lzi", "第1集$https://a.invalid/1.m3u8#第2集$https://a.invalid/2.m3u8"
                               "$$$第1集$https://a.invalid/1.mp4",
                               "https://a.invalid/api.php/provide/vod")
        self.assertEqual(len(lines["m3u8"]), 2)
        self.assertEqual(lines["lzi"][0].url, "https://a.invalid/1.mp4")
        self.assertTrue(lines["m3u8"][0].direct)

    def test_map_vod(self):
        row = {
            "vod_id": 7, "vod_name": "示例影片", "type_name": "电影",
            "vod_year": "2024", "vod_score": "8.0", "vod_pic": "https://a.invalid/p.jpg",
            "vod_play_from": "m3u8", "vod_play_url": "正片$https://a.invalid/p.m3u8",
        }
        title = cms.map_vod(row, {"id": "s_1", "api": "https://a.invalid/api.php/provide/vod"})
        self.assertIsNotNone(title)
        self.assertFalse(cms.is_tv(title))
        self.assertEqual(cms.episode_count(title), 1)

    def test_map_vod_keeps_source_name_for_line_prefix(self):
        row = {
            "vod_id": 11, "vod_name": "源名前缀", "type_name": "电影",
            "vod_play_from": "m3u8", "vod_play_url": "正片$https://a.invalid/p.m3u8",
        }
        api = "https://a.invalid/api.php/provide/vod"
        named = cms.map_vod(row, {"id": "s_1", "name": "本地", "api": api})
        self.assertEqual(named.provider_name, "本地")
        unnamed = cms.map_vod(row, {"id": "s_2", "api": api})
        self.assertEqual(unnamed.provider_name, "s_2")

    def test_merge_titles_across_sources(self):
        def title(provider, name, line, year="2025"):
            return cms.OnlineTitle(
                provider=provider, provider_name=name, vod_id="1",
                title="谷雨", year=year, type_name="电视剧",
                lines={line: [cms.Episode("第1集",
                                          f"https://{provider}.invalid/1.m3u8")]})
        merged = cms.merge_titles([title("p_b", "网络", "lz"),
                                   title("p_a", "本地", "tp")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].provider, "p_a")
        self.assertEqual(list(merged[0].lines), ["tp", "网络：lz"])
        self.assertEqual(len(cms.line_names(merged[0])), 2)

    def test_merge_titles_keeps_different_years_and_same_provider(self):
        def title(provider, name, line, year="2025", vod="1"):
            return cms.OnlineTitle(
                provider=provider, provider_name=name, vod_id=vod,
                title="同名片", year=year, type_name="电影",
                lines={line: [cms.Episode("正片",
                                          f"https://{provider}.invalid/1.m3u8")]})
        self.assertEqual(len(cms.merge_titles([
            title("p_a", "本地", "a", "2024"),
            title("p_b", "网络", "b", "2025")])), 2)
        self.assertEqual(len(cms.merge_titles([
            title("p_a", "本地", "a", "2025", "1"),
            title("p_a", "本地", "b", "2025", "2")])), 2)

    def test_xml_response(self):
        xml = ('<rss><list><video><id>3</id><name>XML片</name><type>电影</type>'
               '<dl><dd flag="m3u8"><![CDATA[正片$https://a.invalid/x.m3u8]]></dd></dl>'
               '</video></list></rss>')
        data = cms._xml_to_json(xml)
        self.assertEqual(data["list"][0]["vod_name"], "XML片")
        self.assertIn("vod_play_url", data["list"][0])

    def test_map_vod_cleans_html_and_keeps_updated(self):
        row = {
            "vod_id": 9, "vod_name": "清洗测试", "type_name": "剧情 / 动作",
            "vod_content": "<p>第一段&nbsp;A</p><br><div>第二段</div>",
            "vod_time": "2026-11-01", "vod_play_from": "m3u8",
            "vod_play_url": "正片$https://a.invalid/p.m3u8",
        }
        title = cms.map_vod(row, {"id": "s_1",
                                  "api": "https://a.invalid/api.php/provide/vod"})
        self.assertIsNotNone(title)
        self.assertEqual(title.updated, "2026-11-01")
        self.assertNotIn("<p>", title.overview)
        self.assertIn("第一段 A", title.overview)
        self.assertIn("第二段", title.overview)


class FakeTest(unittest.TestCase):
    def setUp(self):
        self.brief = {
            "guid": "abc", "title": "示例", "year": "2024", "score": "7.5",
            "poster": "https://a.invalid/p.jpg", "overview": "简介",
            "actor": "", "director": "", "area": "", "remarks": "",
            "type_name": "电视剧", "lines": {"m3u8": [
                {"title": "第1集", "url": "https://a.invalid/1.m3u8"},
                {"title": "第2集", "url": "https://a.invalid/2.m3u8"}]},
            "is_tv": True, "episodes": 2, "line_names": ["m3u8"],
        }

    def test_guids_stable(self):
        self.assertEqual(fake.media_guid("abc"), fake.media_guid("abc"))
        self.assertNotEqual(fake.media_guid("abc"), fake.video_guid("abc"))

    def test_cards_and_episodes(self):
        card = fake.search_card(self.brief)
        self.assertTrue(card["title"].startswith(fake.TAG))
        self.assertEqual(card["type"], "TV")
        self.assertEqual(len(fake.episode_list(self.brief)), 2)
        self.assertEqual(len(fake.season_list(self.brief)), 1)

    def test_tv_strict_card(self):
        card = fake.search_card(self.brief)
        strict = fake.normalize_card(card, "1.4.2 (com.trim.tv; build:1) okHttp/4.9.0")
        self.assertEqual(strict["status"], "1")
        self.assertEqual(strict["release_date"], "2024-01-01")
        self.assertNotIn("genres", strict)
        self.assertNotIn("can_play", strict)

    def test_detail_not_stripped(self):
        detail = fake.detail_item(self.brief)
        strict = fake.normalize_detail(detail, "com.trim.tv")
        self.assertEqual(strict["can_play"], 1)
        self.assertEqual(strict["release_date"], "2024-01-01")

    def test_detail_metadata_mapping(self):
        brief = dict(self.brief, type_name="剧情 / 动作", actor="某演员",
                     director="某导演", updated="2026-11-01",
                     overview="第一段\n第二段")
        detail = fake.detail_item(brief)
        self.assertEqual(detail["genres"], [])
        self.assertIn("分类：剧情 / 动作", detail["overview"])
        self.assertIn("第一段", detail["overview"])
        self.assertIn("主演：某演员", detail["overview"])
        self.assertIn("导演：某导演", detail["overview"])
        self.assertIn("更新：2026-11-01", detail["overview"])
        self.assertEqual(detail["duration"], 0)
        self.assertEqual(detail["runtime"], 0)

    def test_card_overview_is_cleaned_and_bounded(self):
        card = fake.search_card(dict(self.brief, overview="<p>" + "影" * 1500 + "</p>"))
        self.assertNotIn("<p>", card["overview"])
        self.assertLessEqual(len(card["overview"]), 1000)
        strict = fake.normalize_card(card, "com.trim.tv")
        self.assertEqual(strict["status"], "1")

    def test_brief_derives_primary_ext(self):
        title = cms.map_vod(
            {"vod_id": 1, "vod_name": "X", "vod_play_from": "mp4",
             "vod_play_url": "正片$https://a.invalid/x.mp4"},
            {"id": "s_1", "api": "https://a.invalid/api.php/provide/vod"})
        self.assertEqual(fake.brief_of(title, "g")["primary_ext"], "mp4")

    def test_stream_list_uses_ext_without_touching_metadata(self):
        sl = fake.stream_list(dict(self.brief, primary_ext="mp4"), "mg", "vg")
        self.assertTrue(sl["files"][0]["file_name"].endswith(".mp4"))
        video = sl["video_streams"][0]
        self.assertEqual(video["resolution_type"], "1080p")
        self.assertEqual(video["width"], 1920)
        self.assertEqual(video["duration"], 0)
        self.assertEqual(video["bps"], 0)
        self.assertEqual(video["codec_name"], "h264")


    def test_line_display_uses_source_prefix(self):
        self.assertEqual(fake.line_display({"provider_name": "本地"}, "lzm3u8"),
                         "本地：量子")
        self.assertEqual(
            fake.line_display({"provider_name": "本地"}, "网络：lzm3u8"),
            "网络：量子")

    def test_poster_path_uses_short_prefix(self):
        path = fake.poster_path("https://img.invalid/p.jpg")
        self.assertTrue(path.startswith("online/"))
        self.assertTrue(path.endswith(".jpg"))

    def test_detail_posters_use_proxy_path(self):
        detail = fake.detail_item(dict(self.brief, poster="https://img.invalid/p.jpg"))
        self.assertTrue(detail["posters"].startswith("online/"))
        self.assertEqual(detail["backdrops"], detail["posters"])

    def test_map_vod_resolves_relative_poster(self):
        title = cms.map_vod(
            {"vod_id": 3, "vod_name": "X", "vod_pic": "/upload/vod/2026/p.jpg",
             "vod_play_from": "m3u8",
             "vod_play_url": "正片$https://a.invalid/x.m3u8"},
            {"id": "s_1", "api": "https://a.invalid/api.php/provide/vod"})
        self.assertEqual(title.poster, "https://a.invalid/upload/vod/2026/p.jpg")


    def test_person_list_structure(self):
        brief = dict(self.brief, guid="abc", director="张三、李四",
                     actor="王五,赵六/孙七")
        result = fake.person_list(brief)
        self.assertEqual(result["total"], 5)
        jobs = [(row["name"], row["job"], row["order"]) for row in result["list"]]
        self.assertEqual(jobs[0], ("张三", "Director", 1))
        self.assertEqual(jobs[2], ("王五", "Actor", 3))
        self.assertEqual(jobs[4], ("孙七", "Actor", 5))
        for key in ("profile_path", "role", "biography", "item_guid"):
            self.assertIn(key, result["list"][0])

    def test_title_store_drop_clears_indexes(self):
        def make(vod_id):
            return cms.OnlineTitle(
                provider="p", vod_id=vod_id, title="T" + vod_id,
                type_name="电视剧", overview="o",
                lines={"m3u8": [cms.Episode("第1集", f"https://a.invalid/{vod_id}-1.m3u8"),
                                cms.Episode("第2集", f"https://a.invalid/{vod_id}-2.m3u8")]})
        store = cms.TitleStore(max_items=2, ttl=86400)
        store.remember([make("1"), make("2"), make("3")])
        g1 = store.guid_of(make("1"))
        self.assertIsNone(store.get(g1))
        self.assertNotIn(fake.episode_guid(g1, 0), store._episode)
        self.assertNotIn(fake.season_guid(g1), store._season)
        g2 = store.guid_of(make("2"))
        store._drop(g2)
        self.assertIsNone(store.get(g2))
        self.assertNotIn(fake.episode_guid(g2, 1), store._episode)
        self.assertEqual(store.count(), 1)


    def test_stream_list_with_playlist_meta(self):
        meta = {"width": 1920, "height": 1080, "bandwidth": 8000000,
                "variant_count": 3, "duration": 96.0, "has_endlist": True}
        video = fake.stream_list(dict(self.brief), "mg", "vg", meta)["video_streams"][0]
        self.assertEqual(video["resolution_type"], "1080p")
        self.assertEqual(video["width"], 1920)
        self.assertEqual(video["height"], 1080)
        self.assertEqual(video["bps"], 8000000)
        self.assertEqual(video["wrapper"], "HLS")
        sl2 = fake.stream_list(dict(self.brief, primary_ext="mp4"), "mg", "vg",
                               {"width": 1280, "height": 720})
        video2 = sl2["video_streams"][0]
        self.assertEqual(video2["resolution_type"], "720p")
        self.assertEqual(video2["width"], 1280)
        self.assertEqual(video2["height"], 720)
        self.assertEqual(video2["wrapper"], "MP4")
        video3 = fake.stream_list(dict(self.brief), "mg", "vg")["video_streams"][0]
        self.assertEqual(video3["resolution_type"], "1080p")
        self.assertEqual(video3["bps"], 0)
        self.assertEqual(video3["wrapper"], "HLS")


if __name__ == "__main__":
    unittest.main()
