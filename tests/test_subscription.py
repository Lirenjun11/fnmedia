import _bootstrap  
import json
import unittest
from unittest.mock import patch

import config
import subscription

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(value: str) -> str:
    n = int.from_bytes(value.encode(), "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _ALPHABET[rem] + out
    zeros = len(value.encode()) - len(value.encode().lstrip(b"\x00"))
    return "1" * zeros + out


class SubscriptionTest(unittest.TestCase):
    def test_base58_roundtrip(self):
        payload = json.dumps({"cache_time": 7200, "api_site": {}}, ensure_ascii=False)
        encoded = b58encode(payload)
        self.assertEqual(subscription.b58decode(encoded).decode(), payload)

    def test_decode_api_site_config(self):
        payload = {
            "cache_time": 7200,
            "api_site": {
                "example.invalid": {
                    "name": "🎬示例源",
                    "api": "https://example.invalid/api.php/provide/vod/at/json",
                    "detail": "https://example.invalid",
                }
            },
        }
        items, report = subscription.normalize(subscription.decode_payload(b58encode(
            json.dumps(payload, ensure_ascii=False))))
        self.assertEqual(report["added"], 1)
        self.assertFalse(items[0]["enabled"])
        self.assertTrue(items[0]["id"].startswith("s_"))
        self.assertEqual(items[0]["api"], payload["api_site"]["example.invalid"]["api"])
        self.assertEqual(items[0]["key"], "example.invalid")
        self.assertEqual(items[0]["detail"], "https://example.invalid")
        self.assertEqual(items[0]["name"], "🎬示例源")

    def test_decode_libretv_sourcelist(self):
        payload = {"sources": [
            {"name": "A", "url": "https://example.invalid/api.php/provide/vod"},
            {"name": "🔞成人", "url": "https://example.invalid/adult/api.php/provide/vod"},
            {"name": "bad", "url": "ftp://example.invalid/x"},
        ]}
        items, report = subscription.normalize(payload)
        self.assertEqual(len(items), 2)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["adult"], 1)
        self.assertTrue(any(i.get("adult") for i in items))
        self.assertEqual(items[0]["name"], "A")

    def test_tvbox_sites_only_cms(self):
        payload = {"sites": [
            {"key": "a", "name": "CMS", "type": 1,
             "api": "https://example.invalid/api.php/provide/vod"},
            {"key": "b", "name": "XBPQ", "type": 3,
             "api": "https://example.invalid/api.php/provide/vod"},
        ]}
        items, _ = subscription.normalize(payload)
        self.assertEqual(len(items), 1)

    def test_unwrap_cors_proxy_prefix(self):
        inner = "https://example.invalid/api.php/provide/vod"
        wrapped = "https://worker.invalid/?url=https%3A%2F%2Fexample.invalid%2Fapi.php%2Fprovide%2Fvod"
        self.assertEqual(subscription.unwrap_proxy(wrapped), inner)

    def test_intranet_subscription_allowed_by_default(self):
        subscription._public_url("http://192.168.1.10/subscription")

    def test_intranet_subscription_blocked_when_disabled(self):
        blocked = {**config.DEFAULTS, "allow_intranet": False}
        with patch.object(config, "get", return_value=blocked):
            with self.assertRaises(ValueError):
                subscription._public_url("http://192.168.1.10/subscription")

    def test_merge_all_keeps_manual_sources(self):
        def src(sid, api, origin="subscription"):
            return {"id": sid, "name": sid, "api": api,
                    "enabled": True, "from": origin}
        remote = [src("s_new", "https://a.invalid/api.php/provide/vod")]
        manual = src("s_manual", "https://m.invalid/api.php/provide/vod", "manual")
        merged, stats = subscription.merge_all(remote, [manual])
        self.assertEqual(stats["new_sources"], 1)
        self.assertEqual(stats["sources_total"], 2)
        self.assertEqual({s["id"] for s in merged}, {"s_new", "s_manual"})

    def test_merge_keeps_local_enabled_and_manual(self):
        remote = [{"id": "s_1", "name": "新名", "api": "https://example.invalid/api.php/provide/vod",
                   "enabled": False, "from": "subscription"}]
        local = [
            {"id": "s_1", "name": "旧名", "api": "https://example.invalid/old",
             "enabled": True, "from": "manual"},
            {"id": "s_2", "name": "手动", "api": "https://example.invalid/manual",
             "enabled": True, "from": "manual"},
        ]
        merged, stats = subscription.merge(remote, local)
        by_id = {s["id"]: s for s in merged}
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["updated"], 1)
        self.assertTrue(by_id["s_1"]["enabled"])
        self.assertEqual(by_id["s_1"]["name"], "新名")
        self.assertIn("s_2", by_id)


if __name__ == "__main__":
    unittest.main()
