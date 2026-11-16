import _bootstrap
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import fake
import tmdb


def _brief(**kw) -> dict:
    base = {
        "guid": "g1", "title": "星际穿越", "year": "2014",
        "type_name": "科幻片", "is_tv": False, "episodes": 0,
        "poster": "https://source.invalid/p.jpg", "overview": "短",
        "actor": "", "director": "", "score": "0", "area": "美国",
    }
    base.update(kw)
    return base


META = {
    "tmdb_id": 157336, "tmdb_type": "movie", "title": "星际穿越",
    "original_title": "Interstellar",
    "overview": "近未来的地球黄沙遍野，一支探险队穿越虫洞寻找新家园。",
    "vote_average": 8.4, "genres": ["冒险", "剧情", "科幻"],
    "runtime": 169, "release_date": "2014-11-05",
    "poster": "https://image.tmdb.org/t/p/w500/p.jpg",
    "backdrop": "https://image.tmdb.org/t/p/w1280/b.jpg",
    "cast": [{"name": "马修·麦康纳"}, {"name": "安妮·海瑟薇"}],
    "director": "克里斯托弗·诺兰",
    "number_of_seasons": 0, "number_of_episodes": 0,
}


class TmdbBase(unittest.TestCase):
    """每个用例用独立缓存文件与配置，避免污染 state/ 和相互串号。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="tmdb_test_")
        self._orig_file = tmdb._CACHE_FILE
        tmdb._CACHE_FILE = Path(self._dir) / "tmdb_cache.json"
        tmdb._CACHE = {}
        tmdb._LOADED = True          # 跳过读盘，直接空缓存
        tmdb._INFLIGHT = {}
        tmdb._GOOD.update(channel="", until=0.0)
        tmdb._CFG = {}
        tmdb._CFG_TS = 0.0
        self._cfg = config.get()
        self._keep = {k: self._cfg.get(k) for k in (
            "tmdb_enabled", "tmdb_api_key", "tmdb_proxy",
            "source_proxy", "douban_enabled")}

    def tearDown(self):
        tmdb._CACHE_FILE = self._orig_file
        cfg = config.get()
        cfg.update(self._keep)
        config.save(cfg)
        tmdb._CACHE = {}
        tmdb._LOADED = False

    def _config(self, **kw):
        cfg = config.get()
        cfg.update(kw)
        config.save(cfg)
        tmdb._CFG = {}
        tmdb._CFG_TS = 0.0


class TitleTest(unittest.TestCase):
    def test_clean_title_strips_site_suffix(self):
        cases = {
            "沙丘【高清】": "沙丘",
            "沙丘 ·正片": "沙丘",
            "沙丘 国语版": "沙丘",
            "沙丘 (2021)": "沙丘",
            "沙丘：第二部": "沙丘：第二部",
        }
        for raw, want in cases.items():
            self.assertEqual(tmdb.clean_title(raw), want, raw)

    def test_is_tv_brief(self):
        self.assertFalse(tmdb.is_tv_brief({"type_name": "动作片", "is_tv": True}))
        self.assertTrue(tmdb.is_tv_brief({"type_name": "国产剧", "is_tv": False}))
        self.assertTrue(tmdb.is_tv_brief({"type_name": "动漫", "is_tv": False}))
        # 「动画电影」含「电影」，必须先判电影
        self.assertFalse(tmdb.is_tv_brief({"type_name": "动画电影", "is_tv": True}))
        self.assertTrue(tmdb.is_tv_brief({"type_name": "未知", "is_tv": True}))

    def test_cache_key_ignores_punctuation_and_case(self):
        self.assertEqual(tmdb._cache_key("Dune：沙丘", "2021", False),
                         tmdb._cache_key("dune 沙丘", "2021", False))


class PickTest(unittest.TestCase):
    RESULTS = [
        {"id": 1, "title": "星际穿越", "release_date": "2014-11-05",
         "popularity": 30.0},
        {"id": 2, "title": "星际穿越", "release_date": "2024-01-01",
         "popularity": 99.0},
        {"id": 3, "title": "完全无关的高热片", "release_date": "2014-01-01",
         "popularity": 100.0},
    ]

    def test_prefers_matching_year_over_popularity(self):
        best = tmdb._pick(self.RESULTS, "星际穿越", "2014", False)
        self.assertEqual(best["id"], 1)

    def test_rejects_unrelated_results(self):
        self.assertIsNone(tmdb._pick([self.RESULTS[2]], "星际穿越", "2014", False))

    def test_accepts_title_match_without_year(self):
        best = tmdb._pick(self.RESULTS[:2], "星际穿越", "", False)
        self.assertEqual(best["id"], 2)  # 无年份信息时按热度


class EnrichTest(TmdbBase):
    def test_disabled_without_key(self):
        self._config(tmdb_enabled=True, tmdb_api_key="")
        self.assertFalse(tmdb.enabled())
        self.assertIsNone(asyncio.run(tmdb.enrich(_brief())))
        self.assertIsNone(tmdb.cached(_brief()))

    def test_disabled_by_switch(self):
        self._config(tmdb_enabled=False, tmdb_api_key="k" * 32)
        self.assertFalse(tmdb.enabled())

    def test_enrich_writes_and_hits_cache(self):
        self._config(tmdb_enabled=True, tmdb_api_key="k" * 32)
        calls = []

        async def fake_fetch(title, year, is_tv):
            calls.append((title, year, is_tv))
            return dict(META)

        async def run():
            with patch.object(tmdb, "_fetch_meta", new=fake_fetch):
                first = await tmdb.enrich(_brief())
                second = await tmdb.enrich(_brief())
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first["tmdb_id"], 157336)
        self.assertEqual(second["tmdb_id"], 157336)
        self.assertEqual(len(calls), 1)          # 第二次命中缓存，不再请求
        self.assertEqual(calls[0][0], "星际穿越")
        self.assertEqual(calls[0][1], "2014")
        self.assertFalse(calls[0][2])
        self.assertIsNotNone(tmdb.cached(_brief()))
        self.assertTrue(tmdb._CACHE_FILE.exists())   # 已落盘

    def test_negative_cache_suppresses_retry(self):
        self._config(tmdb_enabled=True, tmdb_api_key="k" * 32)
        calls = []

        async def fake_fetch(title, year, is_tv):
            calls.append(title)
            return None

        async def run():
            with patch.object(tmdb, "_fetch_meta", new=fake_fetch):
                await tmdb.enrich(_brief())
                await tmdb.enrich(_brief())

        asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertIsNone(tmdb.cached(_brief()))

    def test_tv_goes_to_tv_search(self):
        self._config(tmdb_enabled=True, tmdb_api_key="k" * 32)
        seen = {}

        async def fake_fetch(title, year, is_tv):
            seen["tv"] = is_tv
            return None

        async def run():
            with patch.object(tmdb, "_fetch_meta", new=fake_fetch):
                await tmdb.enrich(_brief(type_name="国产剧", is_tv=True))

        asyncio.run(run())
        self.assertTrue(seen["tv"])

    def test_enrich_returns_after_budget_without_blocking(self):
        self._config(tmdb_enabled=True, tmdb_api_key="k" * 32)

        async def slow_fetch(title, year, is_tv):
            await asyncio.sleep(0.4)
            return dict(META)

        async def run():
            with patch.object(tmdb, "_fetch_meta", new=slow_fetch):
                started = time.monotonic()
                data = await tmdb.enrich(_brief(), budget=0.05)
                elapsed = time.monotonic() - started
            return data, elapsed

        data, elapsed = asyncio.run(run())
        self.assertIsNone(data)          # 超预算先返回源站数据
        self.assertLess(elapsed, 0.3)    # 没有被后台任务拖住


class ConfigTest(unittest.TestCase):
    def test_key_is_sanitized(self):
        cfg = config.get()
        keep = cfg.get("tmdb_api_key")
        try:
            cfg["tmdb_api_key"] = " 3f9a-bc.0123 中文 \n"
            config.save(cfg)
            self.assertEqual(config.get()["tmdb_api_key"], "3f9a-bc.0123")
        finally:
            cfg["tmdb_api_key"] = keep
            config.save(cfg)


class DetailInjectTest(unittest.TestCase):
    def test_detail_uses_tmdb_fields(self):
        row = fake.detail_item(_brief(), META)
        self.assertIn("星际穿越", row["title"])
        self.assertIn("近未来", row["overview"])       # 源站简介过短被替换
        self.assertIn("分类：冒险 / 剧情 / 科幻", row["overview"])
        self.assertIn("主演：马修·麦康纳 / 安妮·海瑟薇", row["overview"])
        self.assertIn("导演：克里斯托弗·诺兰", row["overview"])
        self.assertEqual(row["genres"], [])            # 客户端契约仍是整型数组
        self.assertEqual(row["vote_average"], "8.4")
        self.assertEqual(row["runtime"], 169)
        self.assertEqual(row["original_title"], "Interstellar")
        self.assertEqual(row["release_date"], "2014-11-05")
        self.assertIn("/online/", row["posters"])
        self.assertIn("/online/", row["backdrops"])
        self.assertNotEqual(row["posters"], row["backdrops"])  # 横竖图分开

    def test_detail_keeps_source_overview_when_long(self):
        long_text = "源站完整剧情简介" * 20
        row = fake.detail_item(_brief(overview=long_text), META)
        self.assertIn("源站完整剧情简介", row["overview"])
        self.assertNotIn("近未来", row["overview"])

    def test_detail_without_meta_unchanged(self):
        row = fake.detail_item(_brief())
        self.assertEqual(row["genres"], [])
        self.assertEqual(row["runtime"], 0)
        self.assertEqual(row["number_of_seasons"], 0)

    def test_history_card_uses_backdrop(self):
        row = fake.history_card(_brief(), {"ts": 10, "duration": 100}, META)
        self.assertIn("/online/", row["poster"])
        self.assertEqual(row["poster"], row["backdrops"])
        self.assertEqual(row["ts"], 10)

    def test_history_card_without_meta_keeps_poster(self):
        row = fake.history_card(_brief(), {"ts": 10, "duration": 100})
        self.assertNotIn("backdrops", row)


class FetchFlowTest(TmdbBase):
    """用本地假 TMDB 验证真实 HTTP 路径：搜索 → 详情 → 演员表 与参数拼接。"""

    def _serve(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append(self.path)
                path = self.path.split("?")[0]
                if path.endswith("/search/movie"):
                    payload = {"results": [
                        {"id": 157336, "title": "星际穿越",
                         "release_date": "2014-11-05", "popularity": 30.0,
                         "poster_path": "/search.jpg"}]}
                elif path.endswith("/movie/157336/credits"):
                    payload = {
                        "cast": [{"id": 1, "name": "马修·麦康纳",
                                  "profile_path": "/a.jpg", "character": "库珀"}],
                        "crew": [{"name": "克里斯托弗·诺兰", "job": "Director"}],
                    }
                elif path.endswith("/movie/157336"):
                    payload = {
                        "id": 157336, "title": "星际穿越",
                        "original_title": "Interstellar",
                        "overview": "穿越虫洞寻找新家园。",
                        "vote_average": 8.4, "runtime": 169,
                        "release_date": "2014-11-05",
                        "genres": [{"name": "冒险"}, {"name": "科幻"}],
                        "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
                    }
                else:
                    payload = {}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, seen

    def test_fetch_meta_end_to_end(self):
        self._config(tmdb_enabled=True, tmdb_api_key="k" * 32,
                     tmdb_proxy="", source_proxy="")
        server, seen = self._serve()
        port = server.server_address[1]
        old_api = tmdb._API
        tmdb._API = f"http://127.0.0.1:{port}/3"
        try:
            meta = asyncio.run(tmdb._fetch_meta("星际穿越", "2014", False))
        finally:
            tmdb._API = old_api
            server.shutdown()
            server.server_close()

        self.assertIsNotNone(meta)
        self.assertEqual(meta["tmdb_id"], 157336)
        self.assertEqual(meta["original_title"], "Interstellar")
        self.assertEqual(meta["genres"], ["冒险", "科幻"])
        self.assertEqual(meta["runtime"], 169)
        self.assertEqual(meta["director"], "克里斯托弗·诺兰")
        self.assertEqual(meta["cast"][0]["name"], "马修·麦康纳")
        self.assertIn("w500", meta["poster"])
        self.assertIn("w1280", meta["backdrop"])
        self.assertIn("w185", meta["cast"][0]["profile"])
        # 三次请求都带 api_key 与中文语言
        self.assertEqual(len(seen), 3)
        self.assertTrue(all("api_key=" + "k" * 32 in row for row in seen))
        self.assertTrue(all("language=zh-CN" in row for row in seen))

    def test_fetch_meta_rejects_401_without_retry(self):
        self._config(tmdb_enabled=True, tmdb_api_key="k" * 32,
                     tmdb_proxy="", source_proxy="")
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(1)
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        old_api = tmdb._API
        tmdb._API = f"http://127.0.0.1:{port}/3"
        try:
            with patch.object(tmdb, "_channels",
                              return_value=[("a", ""), ("b", "http://127.0.0.1:9")]):
                meta = asyncio.run(tmdb._fetch_meta("星际穿越", "2014", False))
        finally:
            tmdb._API = old_api
            server.shutdown()
            server.server_close()
        self.assertIsNone(meta)
        self.assertEqual(len(hits), 1)   # Key 无效不再换通道重试


class AdminRouteTest(unittest.TestCase):
    """管理接口必须真的注册上，且未配置 Key 时给可读错误而不是发请求。"""

    def setUp(self):
        self._keep = {k: config.get().get(k)
                      for k in ("tmdb_enabled", "tmdb_api_key")}
        cfg = config.get()
        cfg.update({"tmdb_enabled": True, "tmdb_api_key": ""})
        config.save(cfg)
        tmdb._CFG = {}
        tmdb._CFG_TS = 0.0

    def tearDown(self):
        cfg = config.get()
        cfg.update(self._keep)
        config.save(cfg)
        tmdb._CFG = {}
        tmdb._CFG_TS = 0.0

    def test_routes_require_token_and_report_missing_key(self):
        from fastapi.testclient import TestClient
        import app as appmod

        with TestClient(appmod.app) as client:
            self.assertEqual(client.get("/v/api/ext/tmdb/status").status_code, 401)
            headers = {"x-admin-token": config.token()}
            status = client.get("/v/api/ext/tmdb/status", headers=headers)
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["data"]["has_key"])
            probe = client.post("/v/api/ext/tmdb/test", headers=headers, json={})
            self.assertEqual(probe.status_code, 200)
            body = probe.json()["data"]
            self.assertFalse(body["ok"])
            self.assertIn("Key", body["error"])


if __name__ == "__main__":
    unittest.main()
