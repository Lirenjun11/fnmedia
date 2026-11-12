import _bootstrap  # noqa: F401
import asyncio
import unittest
from unittest.mock import patch

import httpx

import cms

SOURCE = {"id": "s_1", "api": "https://a.invalid/api.php/provide/vod"}
SOURCE2 = {"id": "s_2", "api": "https://b.invalid/api.php/provide/vod"}


def _rows():
    return [{
        "vod_id": 1, "vod_name": "测试片", "vod_play_from": "m3u8",
        "vod_play_url": "正片$https://a.invalid/1.m3u8",
    }]


def _title(provider="s_1"):
    return cms.OnlineTitle(
        provider=provider, vod_id="1", title="测试片",
        lines={"线路一": [cms.Episode("正片", "https://a.invalid/1.m3u8")]})


class SearchRetryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cms._CACHE.clear()
        cms._INFLIGHT.clear()

    def tearDown(self):
        cms._CACHE.clear()
        cms._INFLIGHT.clear()

    async def test_empty_first_search_is_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            rows = [] if calls["n"] == 1 else _rows()
            return httpx.Response(200, json={"code": 1, "list": rows})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await cms.search_all(client, [SOURCE], "重试", timeout=1.0)
            second = await cms.search_all(client, [SOURCE], "重试", timeout=1.0)
        finally:
            await client.aclose()
        self.assertEqual(len(first), 0)
        self.assertEqual(len(second), 1)
        self.assertEqual(calls["n"], 2)

    async def test_nonempty_result_is_cached(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"code": 1, "list": _rows()})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await cms.search_all(client, [SOURCE], "缓存", timeout=1.0)
            second = await cms.search_all(client, [SOURCE], "缓存", timeout=1.0)
        finally:
            await client.aclose()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(calls["n"], 1)

    async def test_single_source_failure_does_not_drop_other_sources(self):
        async def fake_search(client, source, keyword, timeout):
            if source["id"] == "s_1":
                raise RuntimeError("boom")
            await asyncio.sleep(0.01)
            return [_title("s_2")]

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200)))
        try:
            with patch.object(cms, "search_source", side_effect=fake_search):
                result = await cms.search_all(
                    client, [SOURCE, SOURCE2], "异常", timeout=1.0)
        finally:
            await client.aclose()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].provider, "s_2")
