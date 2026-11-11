import _bootstrap
import asyncio
import unittest

import httpx

import douban


def make_client(entries, status=200):
    def handler(request):
        return httpx.Response(status, json=entries)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class DoubanTest(unittest.TestCase):
    def setUp(self):
        douban._CACHE.clear()

    def test_hit_upgrades_to_large_poster(self):
        client = make_client([{"title": "不能说的秘密", "year": "2026",
                               "img": "https://img1.doubanio.com/view/photo/"
                                      "s_ratio_poster/public/p1.jpg",
                               "id": "368", "type": "movie"}])
        detail = {"backdrops": "/online/x.jpg", "posters": "/online/x.jpg"}
        out = asyncio.run(douban.enrich_poster(client, detail,
                                               "不能说的秘密", "2026"))
        self.assertEqual(out["posters"],
                         "https://img1.doubanio.com/view/photo/"
                         "l_ratio_poster/public/p1.jpg")
        self.assertEqual(out["backdrops"], out["posters"])

    def test_year_gap_keeps_original(self):
        client = make_client([{"title": "不能说的秘密", "year": "2010",
                               "img": "https://img1.doubanio.com/x.jpg"}])
        detail = {"posters": "/online/x.jpg"}
        out = asyncio.run(douban.enrich_poster(client, detail,
                                               "不能说的秘密", "2026"))
        self.assertEqual(out["posters"], "/online/x.jpg")

    def test_network_failure_silent(self):
        client = make_client([], status=500)
        detail = {"posters": "/online/x.jpg"}
        out = asyncio.run(douban.enrich_poster(client, detail,
                                               "完全不同的片子", "2024"))
        self.assertEqual(out["posters"], "/online/x.jpg")


if __name__ == "__main__":
    unittest.main()
