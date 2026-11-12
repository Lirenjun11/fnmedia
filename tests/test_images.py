"""详情页图片字段必须能被客户端取到（否则顶部背景一片黑）。"""
import _bootstrap  # noqa: F401
import unittest

import fake


class AdoptImagesTest(unittest.TestCase):
    def test_external_backdrop_goes_through_proxy(self):
        row = {"poster": "/online/aaa.jpg", "posters": "",
               "backdrops": "https://img9.doubanio.com/view/photo/l/public/p1.jpg",
               "logos": ""}
        out = fake.adopt_images(row)
        self.assertTrue(out["backdrops"].startswith("/online/"))
        self.assertNotIn("doubanio", out["backdrops"])
        # 原本为空的海报位用同一张代理图补齐。
        self.assertEqual(out["posters"], "/online/aaa.jpg")

    def test_official_hash_falls_back_to_cms_poster(self):
        row = {"poster": "/online/aaa.jpg", "backdrops": "b3f1c2d4e5",
               "posters": "b3f1c2d4e5", "logos": ""}
        out = fake.adopt_images(row)
        self.assertEqual(out["backdrops"], "/online/aaa.jpg")
        self.assertEqual(out["posters"], "/online/aaa.jpg")

    def test_relative_path_is_kept(self):
        row = {"poster": "/online/aaa.jpg",
               "backdrops": "/v/api/v1/sys/img/xx.jpg", "posters": ""}
        out = fake.adopt_images(row)
        self.assertEqual(out["backdrops"], "/v/api/v1/sys/img/xx.jpg")

    def test_json_array_multiple_urls(self):
        row = {"poster": "", "posters": "",
               "backdrops": '["https://a.invalid/1.jpg","https://b.invalid/2.jpg"]'}
        out = fake.adopt_images(row)
        self.assertEqual(out["backdrops"].count("/online/"), 2)
        self.assertNotIn("http", out["backdrops"])
        self.assertTrue(out["poster"].startswith("/online/"))

    def test_empty_stays_empty_when_no_poster(self):
        row = {"poster": "", "backdrops": "", "posters": ""}
        out = fake.adopt_images(row)
        self.assertEqual(out["backdrops"], "")
        self.assertEqual(out["posters"], "")

    def test_logos_also_proxied(self):
        row = {"poster": "/online/aaa.jpg", "backdrops": "/online/aaa.jpg",
               "posters": "/online/aaa.jpg",
               "logos": "https://img.invalid/logo.png"}
        out = fake.adopt_images(row)
        self.assertTrue(out["logos"].startswith("/online/"))


class DetailImageRouteTest(unittest.TestCase):
    """搜索链路详情：借库/豆瓣塞进来的外链必须换成站点代理地址。"""

    def test_borrowed_backdrop_is_rewritten(self):
        import cms as cms_mod
        import app as appmod
        from unittest.mock import patch
        from fastapi.testclient import TestClient

        title = cms_mod.OnlineTitle(
            provider="s_1", vod_id="img-1", title="谷雨", type_name="电视剧",
            poster="https://cdn.invalid/p.jpg", year="2026",
            lines={"lz": [cms_mod.Episode("第1集", "https://a.invalid/1.m3u8")]})
        external = "https://img9.doubanio.com/view/photo/l/public/p2.jpg"

        def fake_enrich(detail, title, year):
            detail["backdrops"] = external
            detail["posters"] = external
            return detail

        with patch("borrow.enrich_detail", side_effect=fake_enrich):
            with TestClient(appmod.app) as client:
                client.get("/v/online-admin")
                brief = appmod.app.state.store.remember([title])[0]
                data = client.get(
                    f"/v/api/v1/item/{brief['guid']}",
                    headers={"User-Agent": "fnos-media/1.8.0 (Android 14)"},
                ).json()["data"]
        self.assertTrue(data["backdrops"].startswith("/online/"))
        self.assertNotIn("doubanio", data["backdrops"])
        self.assertTrue(data["posters"].startswith("/online/"))


if __name__ == "__main__":
    unittest.main()
