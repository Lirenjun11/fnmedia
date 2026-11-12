import _bootstrap
import unittest

from webui_gateway import APP_PREFIX, _rewrite


def head(target: str) -> bytes:
    return f"GET {target} HTTP/1.1\r\nHost: nas\r\n\r\n".encode()


class GatewayRewriteTest(unittest.TestCase):
    def test_desktop_root_maps_to_admin(self):
        for target in ("/app/fnmedia-ext/", "/app/fnmedia-ext", "/"):
            out = _rewrite(head(target))
            self.assertIn(b"GET /v/online-admin", out, target)

    def test_malformed_request_is_untouched(self):
        out = _rewrite(head(""))
        self.assertIn(b"HTTP/1.1", out)

    def test_prefixed_assets_are_stripped(self):
        out = _rewrite(head("/app/fnmedia-ext/v/ext/assets/app.js"))
        self.assertIn(b"GET /v/ext/assets/app.js", out)

    def test_official_paths_pass_through(self):
        out = _rewrite(head("/v/api/v1/search/list?q=x"))
        self.assertIn(b"GET /v/api/v1/search/list?q=x", out)
        self.assertNotIn(b"online-admin", out)

    def test_prefix_constant(self):
        self.assertEqual(APP_PREFIX, "/app/fnmedia-ext")


if __name__ == "__main__":
    unittest.main()
