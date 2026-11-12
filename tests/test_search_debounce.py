import _bootstrap  # noqa: F401
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app as appmod
import cms
import config

SOURCE = {
    "id": "s_1", "name": "t",
    "api": "https://a.invalid/api.php/provide/vod",
    "enabled": True,
}


def _request(cookie="sid=1", host="127.0.0.1"):
    return SimpleNamespace(headers={"cookie": cookie},
                           client=SimpleNamespace(host=host))


def _set_config(**overrides):
    cfg = config.get()
    cfg["sources"] = [dict(SOURCE)]
    cfg["search_debounce_ms"] = 40
    cfg.update(overrides)
    config.save(cfg)


async def _local_response(request):
    return JSONResponse({"code": 0, "data": [{"guid": "local-1"}]})


class SearchDebounceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        appmod._SEARCH_LATEST.clear()
        _set_config()

    def tearDown(self):
        appmod._SEARCH_LATEST.clear()
        cfg = config.get()
        cfg["sources"] = []
        cfg["search_debounce_ms"] = 200
        config.save(cfg)

    async def test_only_last_query_in_a_burst_survives(self):
        results = await asyncio.gather(*[
            appmod._search_is_latest(_request(), 40) for _ in range(4)
        ])
        self.assertEqual(sum(1 for ok in results if ok), 1)
        self.assertTrue(results[-1])

    async def test_different_sessions_are_isolated(self):
        results = await asyncio.gather(
            appmod._search_is_latest(_request(cookie="a"), 40),
            appmod._search_is_latest(_request(cookie="b"), 40),
        )
        self.assertEqual(results, [True, True])

    async def test_no_cookie_falls_back_to_host(self):
        req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
        self.assertTrue(await appmod._search_is_latest(req, 1))

    def test_skipped_request_returns_local_without_cms(self):
        search = AsyncMock(return_value=[])
        with TestClient(appmod.app) as client:
            with patch.object(appmod, "_passthrough", new=_local_response), \
                 patch.object(appmod, "_search_is_latest",
                              new=AsyncMock(return_value=False)), \
                 patch.object(cms, "search_all", new=search):
                resp = client.get("/v/api/v1/search/list?q=mut")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["guid"] for row in resp.json()["data"]],
                         ["local-1"])
        search.assert_not_called()

    def test_last_request_queries_cms(self):
        search = AsyncMock(return_value=[])
        with TestClient(appmod.app) as client:
            with patch.object(appmod, "_passthrough", new=_local_response), \
                 patch.object(appmod, "_search_is_latest",
                              new=AsyncMock(return_value=True)), \
                 patch.object(cms, "search_all", new=search):
                resp = client.get("/v/api/v1/search/list?q=mutt")
        self.assertEqual(resp.status_code, 200)
        search.assert_called_once()

    def test_zero_debounce_skips_waiting(self):
        _set_config(search_debounce_ms=0)
        latest = AsyncMock(return_value=True)
        with TestClient(appmod.app) as client:
            with patch.object(appmod, "_passthrough", new=_local_response), \
                 patch.object(appmod, "_search_is_latest", new=latest), \
                 patch.object(cms, "search_all", new=AsyncMock(return_value=[])):
                client.get("/v/api/v1/search/list?q=mutou")
        latest.assert_not_called()
