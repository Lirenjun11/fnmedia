import _bootstrap
import unittest

import netguard


class NetguardTest(unittest.TestCase):
    def test_public_target_always_allowed(self):
        for url in ("https://8.8.8.8/a.ts", "http://1.1.1.1/x.m3u8"):
            self.assertEqual(
                netguard.validate_target(url, allow_intranet=False), url)

    def test_intranet_allowed_when_enabled(self):
        for url in ("http://127.0.0.1/x.ts", "http://localhost/x.ts",
                    "http://192.168.1.10/x.ts", "http://10.0.0.5:8080/a.m3u8",
                    "http://172.16.0.1/x.mp4", "http://[::1]/x.ts",
                    "http://[fd00::1]/x.ts"):
            self.assertEqual(
                netguard.validate_target(url, allow_intranet=True), url)

    def test_intranet_blocked_when_disabled(self):
        for url in ("http://127.0.0.1/x.ts", "http://localhost/x.ts",
                    "http://192.168.1.10/x.ts", "http://[::1]/x.ts"):
            with self.assertRaises(ValueError):
                netguard.validate_target(url, allow_intranet=False)

    def test_metadata_and_reserved_always_blocked(self):
        for url in ("http://169.254.169.254/latest/meta-data",
                    "http://0.0.0.0/x.ts", "http://240.0.0.1/x.ts",
                    "http://224.0.0.1/x.ts", "file:///etc/passwd",
                    "ftp://example.com/x.ts"):
            with self.assertRaises(ValueError, msg=url):
                netguard.validate_target(url, allow_intranet=True)


if __name__ == "__main__":
    unittest.main()
