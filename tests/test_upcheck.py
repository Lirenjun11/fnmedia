import _bootstrap
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app as appmod
import config
import upcheck


def release(tag="v0.1.1", draft=False, prerelease=False, status=200):
    payload = {
        "tag_name": tag, "draft": draft, "prerelease": prerelease,
        "html_url": "https://github.com/Lirenjun11/fnmedia/releases/tag/" + tag,
        "body": "- 修复若干问题",
        "published_at": "2026-11-10T00:00:00Z",
        "assets": [{"name": "fnos_media_ext-0.1.1.fpk",
                    "browser_download_url": "https://example.invalid/fpk"}],
    }
    def handler(request):
        assert request.url.host == "api.github.com"
        return httpx.Response(status, json=payload if status == 200 else {})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class UpcheckTest(unittest.TestCase):
    def setUp(self):
        upcheck._cache.clear()

    def test_version_compare(self):
        self.assertTrue(upcheck.is_newer("0.1.0", "v0.1.1"))
        self.assertFalse(upcheck.is_newer("0.1.0", "v0.1.0"))
        self.assertFalse(upcheck.is_newer("0.2.0", "v0.1.9"))
        self.assertFalse(upcheck.is_newer("0.1.0", "v0.1.1-beta1"))
        self.assertTrue(upcheck.is_newer("", "v0.1.0"))
        self.assertTrue(upcheck.is_newer("0.1.6", "v0.1.6.1"))
        self.assertFalse(upcheck.is_newer("0.1.6.1", "v0.1.6.1"))
        self.assertTrue(upcheck.is_newer("0.1.6.1", "v0.1.7"))
        self.assertFalse(upcheck.is_newer("0.1.7", "v0.1.6.1"))

    def test_safe_repo(self):
        self.assertEqual(upcheck.safe_repo("owner/repo"), "owner/repo")
        self.assertEqual(upcheck.safe_repo("../evil"), upcheck._DEFAULT_REPO)
        self.assertEqual(upcheck.safe_repo(""), upcheck._DEFAULT_REPO)

    def test_default_repo_points_to_fork(self):
        self.assertEqual(upcheck._DEFAULT_REPO, "Lirenjun11/fnmedia")
        self.assertEqual(config.DEFAULTS["update_repo"], "Lirenjun11/fnmedia")

    def test_new_release_found(self):
        async def run():
            async with release() as client:
                return await upcheck.check("Lirenjun11/fnmedia", "0.1.0",
                                          client=client)
        result = asyncio.run(run())
        self.assertTrue(result["has_update"])
        self.assertEqual(result["latest"], "0.1.1")
        self.assertEqual(len(result["assets"]), 1)
        self.assertEqual(result["error"], "")

    def test_prerelease_and_404_are_quiet(self):
        async def pre():
            async with release("v0.2.0-beta1", prerelease=True) as client:
                return await upcheck.check("owner/repo", "0.1.0", client=client)
        self.assertEqual(asyncio.run(pre())["error"], "unstable")

        async def missing():
            async with release(status=404) as client:
                return await upcheck.check("owner/repo2", "0.1.0", client=client)
        self.assertEqual(asyncio.run(missing())["error"], "no_release")

    def test_local_newer_than_remote(self):
        async def run():
            async with release("v0.1.0") as client:
                return await upcheck.check("owner/local", "0.1.1", client=client)
        result = asyncio.run(run())
        self.assertFalse(result["has_update"])
        self.assertEqual(result["latest"], "0.1.1")

    def test_cache_and_force(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={
                "tag_name": "v0.1.1", "draft": False, "prerelease": False,
                "html_url": "https://example.invalid/r", "body": "notes",
                "published_at": "", "assets": []})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run():
            first = await upcheck.check("owner/cached", "0.1.0", client=client)
            second = await upcheck.check("owner/cached", "0.1.0", client=client)
            third = await upcheck.check("owner/cached", "0.1.0",
                                        force=True, client=client)
            await client.aclose()
            return first, second, third
        first, second, third = asyncio.run(run())
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(third["cached"])

    def test_route_requires_token_and_returns_result(self):
        fake_result = {"current": "0.1.0", "latest": "0.1.1",
                       "has_update": True, "html_url": "https://example.invalid/r",
                       "notes": "notes", "assets": [], "published_at": "",
                       "error": "", "cached": False}
        with patch.object(upcheck, "check", new=AsyncMock(return_value=fake_result)):
            with TestClient(appmod.app) as client:
                self.assertEqual(
                    client.get("/v/api/ext/update/check").status_code, 401)
                client.get("/v/online-admin")
                resp = client.get("/v/api/ext/update/check")
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.json()["data"]["has_update"])


if __name__ == "__main__":
    unittest.main()
