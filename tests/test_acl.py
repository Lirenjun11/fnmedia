import _bootstrap
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import acl
import app as appmod
import charts
import cms
import config
import fake
import vlib


class FakeRequest:
    def __init__(self, headers=None, host="127.0.0.1"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = SimpleNamespace(host=host)
        self.cookies = {}


def brief_of(*, provider="s_1", lines=None, mapping=None, names=None):
    lines = lines or {"L1": [{"url": "https://a.invalid/1.m3u8"}]}
    names = names or list(lines)
    data = {
        "guid": "g1", "provider": provider, "provider_name": provider,
        "title": "测试片", "lines": lines, "line_names": names,
        "episodes": max((len(v) for v in lines.values()), default=1),
        "is_tv": False,
    }
    if mapping is not None:
        data["line_sources"] = mapping
    return data


class AclTest(unittest.TestCase):
    def setUp(self):
        cfg = config.get()
        self._orig = {
            "sources": list(cfg.get("sources") or []),
            "acl_enabled": cfg.get("acl_enabled"),
            "acl_subjects": list(cfg.get("acl_subjects") or []),
            "acl_grants": dict(cfg.get("acl_grants") or {}),
            "acl_default_visible": cfg.get("acl_default_visible"),
            "acl_default_online": cfg.get("acl_default_online"),
        }
        cfg.update({
            "sources": [
                {"id": "s_1", "name": "源一", "api": "https://a.invalid/api",
                 "enabled": True},
                {"id": "s_2", "name": "源二", "api": "https://b.invalid/api",
                 "enabled": True},
                {"id": "s_3", "name": "源三", "api": "https://c.invalid/api",
                 "enabled": False},
            ],
            "enabled_limit": 10,
            "acl_enabled": False,
            "acl_subjects": [{"id": "u1", "name": "客厅", "source": "manual"}],
            "acl_grants": {},
            "acl_default_visible": True,
            "acl_default_online": True,
        })
        config.save(cfg)
        acl.invalidate()

    def tearDown(self):
        cfg = config.get()
        cfg.update(self._orig)
        config.save(cfg)
        acl.invalidate()

    def enable(self, grants=None, visible=True, online=True):
        cfg = config.get()
        cfg["acl_enabled"] = True
        cfg["acl_grants"] = grants or {}
        cfg["acl_default_visible"] = visible
        cfg["acl_default_online"] = online
        config.save(cfg)
        acl.invalidate()

    def as_user(self, uid):
        return acl.push(acl.Identity(uid=uid, name=uid, via="session"))

    # ---------------------------------------------------------- 基础行为
    def test_disabled_keeps_legacy_source_live_behaviour(self):
        live = brief_of(provider="s_1")
        dead = brief_of(provider="s_3")
        self.assertIs(acl.filter_brief(live), live)
        self.assertIsNone(acl.filter_brief(dead))
        self.assertEqual(len(acl.sources()), 2)

    def test_default_allow_keeps_everything_visible(self):
        """默认策略放行：开启总开关不会让任何账号瞬间失去内容。"""
        self.enable(grants={})
        token = self.as_user("u1")
        try:
            self.assertIsNotNone(acl.filter_brief(brief_of(provider="s_1")))
            self.assertEqual(len(acl.sources()), 2)
            self.assertTrue(acl.lib_allowed("chart:movie"))
        finally:
            acl.reset(token)

    def test_default_deny_hides_everything(self):
        self.enable(grants={}, visible=False, online=False)
        token = self.as_user("u1")
        try:
            self.assertIsNone(acl.filter_brief(brief_of(provider="s_1")))
            self.assertEqual(acl.sources(), [])
            self.assertFalse(acl.lib_allowed("chart:movie"))
        finally:
            acl.reset(token)

    def test_empty_libs_means_none(self):
        self.enable(grants={"u1": {"libs": [], "sources": ["s_1"]}})
        token = self.as_user("u1")
        try:
            self.assertFalse(acl.lib_allowed("chart:movie"))
            self.assertIsNotNone(acl.filter_brief(brief_of(provider="s_1")))
        finally:
            acl.reset(token)

    def test_online_off_blocks_all_sources(self):
        self.enable(grants={"u1": {"online": False, "libs": ["chart:movie"],
                                   "sources": ["s_1"]}})
        token = self.as_user("u1")
        try:
            self.assertEqual(acl.sources(), [])
            self.assertIsNone(acl.filter_brief(brief_of(provider="s_1")))
            self.assertTrue(acl.lib_allowed("chart:movie"))
        finally:
            acl.reset(token)

    def test_unconfigured_account_uses_defaults(self):
        self.enable(grants={"u1": {"libs": [], "sources": []}},
                    visible=True, online=True)
        token = self.as_user("u2")
        try:
            # u2 没配过 -> 按默认放行；u1 明确配成「都不给」。
            self.assertTrue(acl.lib_allowed("chart:movie"))
            self.assertEqual(len(acl.sources()), 2)
        finally:
            acl.reset(token)

    def test_line_level_filter_uses_line_sources(self):
        self.enable(grants={"u1": {"libs": [], "sources": ["s_1"]}})
        lines = {
            "L1": [{"url": "https://a.invalid/1.m3u8"}],
            "源二：L1": [{"url": "https://b.invalid/1.m3u8"}],
        }
        brief = brief_of(provider="s_1", lines=lines,
                         mapping={"L1": "s_1", "源二：L1": "s_2"})
        token = self.as_user("u1")
        try:
            kept = acl.filter_brief(brief)
            self.assertEqual(kept["line_names"], ["L1"])
            self.assertEqual(list(kept["lines"]), ["L1"])
            self.assertEqual(kept["episodes"], 1)
            # 原 brief 不被就地修改（它来自全局 store）。
            self.assertEqual(len(brief["line_names"]), 2)
        finally:
            acl.reset(token)

    def test_missing_mapping_falls_back_to_primary_provider(self):
        self.enable(grants={"u1": {"libs": [], "sources": ["s_3"]}})
        token = self.as_user("u1")
        try:
            # 未授权 s_1 且没有线路映射：整条不可用。
            self.assertIsNone(acl.filter_brief(brief_of(provider="s_1")))
        finally:
            acl.reset(token)

    def test_sources_intersect_global_enabled(self):
        self.enable(grants={"u1": {"libs": [],
                                   "sources": ["s_1", "s_3"]}})
        token = self.as_user("u1")
        try:
            self.assertEqual([s["id"] for s in acl.sources()], ["s_1"])
        finally:
            acl.reset(token)

    def test_lib_wildcard(self):
        self.enable(grants={"u1": {"libs": ["*"], "sources": []}})
        token = self.as_user("u1")
        try:
            self.assertTrue(acl.lib_allowed("chart:movie"))
            self.assertTrue(acl.lib_allowed("catalog:anime"))
        finally:
            acl.reset(token)

    # ---------------------------------------------------------- 身份
    def test_header_identity_wins(self):
        self.enable(grants={"u9": {"libs": ["chart:tv"], "sources": []}})
        ident = acl.identify(FakeRequest({"x-user-id": "u9"}))
        self.assertEqual((ident.uid, ident.via), ("u9", "header"))

    def test_unknown_without_credentials(self):
        self.enable(grants={"u1": {"libs": ["chart:movie"], "sources": []}},
                    visible=False, online=False)
        ident = acl.identify(FakeRequest({}))
        self.assertFalse(ident.known)
        self.assertEqual(ident.reason, "no_credential")
        token = acl.push(ident)
        try:
            self.assertFalse(acl.lib_allowed("chart:movie"))
        finally:
            acl.reset(token)

    def test_identity_cache_is_invalidated_on_grant_change(self):
        req = FakeRequest({"x-user-id": "u1"})
        self.enable(grants={"u1": {"libs": [], "sources": []}})
        self.assertTrue(acl.identify(req).uid == "u1")
        cfg = config.get()
        cfg["acl_grants"] = {"u1": {"libs": ["chart:movie"], "sources": []}}
        config.save(cfg)
        acl.invalidate("u1")
        token = acl.push(acl.identify(req))
        try:
            self.assertTrue(acl.lib_allowed("chart:movie"))
        finally:
            acl.reset(token)

    # ---------------------------------------------------------- 配置校验
    def test_config_dedup_and_limits(self):
        cfg = config.get()
        cfg["acl_grants"] = {
            "u1": {"libs": ["chart:movie", "chart:movie", ""],
                   "sources": ["s_1", "s_1"]},
        }
        saved = config.save(cfg)
        self.assertEqual(saved["acl_grants"]["u1"]["libs"], ["chart:movie"])
        self.assertEqual(saved["acl_grants"]["u1"]["sources"], ["s_1"])
        # 设备绑定已移除；旧配置里残留的键不会重新落盘。
        self.assertNotIn("acl_devices", saved)

    # ---------------------------------------------------------- 接口
    def test_mediadb_list_only_returns_granted_libs(self):
        now = 1
        charts.chart_cache._apply({
            "updated_at": now, "provider_ok": 1, "provider_total": 1,
            "charts": {"movie": [{"id": "m1", "guid": charts.chart_guid(
                "movie", "m1"), "cat": "movie", "title": "电影一",
                "rating": 8.0, "cover": "", "year": "2025", "tags": []}],
                "tv": [{"id": "t1", "guid": charts.chart_guid("tv", "t1"),
                        "cat": "tv", "title": "剧集一", "rating": 8.0,
                        "cover": "", "year": "2025", "tags": []}]},
        })
        self.enable(grants={"u1": {"libs": ["chart:movie"], "sources": []}})
        upstream = {"code": 0, "msg": "", "data": [{"guid": "local-1"}]}
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse(upstream))):
            with TestClient(appmod.app) as client:
                data = client.get("/v/api/v1/mediadb/list",
                                  headers={"x-user-id": "u1"}).json()["data"]
        guids = [row["guid"] for row in data]
        self.assertIn(vlib.CHART_LIB_GUIDS["movie"], guids)
        self.assertNotIn(vlib.CHART_LIB_GUIDS["tv"], guids)
        self.assertIn("local-1", guids)

    def test_mdb_detail_denied_for_ungranted_lib(self):
        charts.chart_cache._apply({
            "updated_at": 1, "provider_ok": 1, "provider_total": 1,
            "charts": {"tv": [{"id": "t1", "guid": charts.chart_guid(
                "tv", "t1"), "cat": "tv", "title": "剧集一", "rating": 8.0,
                "cover": "", "year": "2025", "tags": []}]},
        })
        self.enable(grants={"u1": {"libs": ["chart:movie"], "sources": []}})
        upstream = {"code": -6, "msg": "此媒体库不存在", "data": None}
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse(upstream, status_code=404))):
            with TestClient(appmod.app) as client:
                resp = client.get(f"/v/api/v1/mdb/{vlib.CHART_LIB_GUIDS['tv']}",
                                  headers={"x-user-id": "u1"})
        self.assertEqual(resp.status_code, 404)

    def test_search_only_uses_granted_sources(self):
        self.enable(grants={"u1": {"libs": [], "sources": ["s_2"]}})
        seen = []

        async def fake_search(client, sources, keyword, **kwargs):
            seen.extend(s["id"] for s in sources)
            return []

        upstream = {"code": 0, "msg": "", "data": []}
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse(upstream))), \
                patch("cms.search_all", fake_search):
            with TestClient(appmod.app) as client:
                client.get("/v/api/v1/search/list?q=测试",
                           headers={"x-user-id": "u1"})
        self.assertEqual(seen, ["s_2"])

    def test_admin_grant_endpoint(self):
        token = config.token()
        headers = {"x-admin-token": token}
        with TestClient(appmod.app) as client:
            resp = client.post("/v/api/ext/acl/grant",
                               headers=headers,
                               json={"uid": "u1", "libs": ["chart:movie"],
                                     "sources": ["s_1"]})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            self.assertEqual(data["grants"]["u1"]["sources"], ["s_1"])

    def test_enable_requires_grant_when_defaults_deny(self):
        """默认全禁 + 一条授权都没有 = 开启即全员无内容，必须拦住。"""
        cfg = config.get()
        cfg["acl_default_visible"] = False
        cfg["acl_default_online"] = False
        config.save(cfg)
        token = config.token()
        with TestClient(appmod.app) as client:
            resp = client.patch("/v/api/ext/acl/config",
                                headers={"x-admin-token": token},
                                json={"enabled": True})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("默认策略是全禁", resp.json()["msg"])

    def test_enable_is_allowed_when_defaults_allow(self):
        token = config.token()
        with TestClient(appmod.app) as client:
            resp = client.patch("/v/api/ext/acl/config",
                                headers={"x-admin-token": token},
                                json={"enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"]["enabled"])

    def test_enable_accepts_both_field_names(self):
        """管理页发 enabled、配置键叫 acl_enabled，两种都得能开。"""
        self.enable(grants={"u1": {"libs": ["chart:movie"], "sources": []}})
        cfg = config.get()
        cfg["acl_enabled"] = False
        config.save(cfg)
        token = config.token()
        with TestClient(appmod.app) as client:
            for body in ({"enabled": True}, {"acl_enabled": True}):
                cfg = config.get()
                cfg["acl_enabled"] = False
                config.save(cfg)
                resp = client.patch("/v/api/ext/acl/config",
                                    headers={"x-admin-token": token},
                                    json=body)
                self.assertEqual(resp.status_code, 200, body)
                self.assertTrue(resp.json()["data"]["enabled"], body)

    def test_delete_subject_also_drops_grants(self):
        self.enable(grants={"u1": {"libs": ["chart:movie"], "sources": ["s_1"]}})
        token = config.token()
        with TestClient(appmod.app) as client:
            resp = client.delete("/v/api/ext/acl/subjects/u1",
                                 headers={"x-admin-token": token})
            data = resp.json()["data"]
        self.assertEqual(resp.status_code, 200)
        # 只删 acl_subjects 会被「仍有授权的 uid」重新补回清单，必须连带清掉。
        self.assertNotIn("u1", [s["id"] for s in data["subjects"]])
        self.assertNotIn("u1", data["grants"])

    def test_export_has_no_device_bindings(self):
        token = config.token()
        with TestClient(appmod.app) as client:
            data = client.get("/v/api/ext/export",
                              headers={"x-admin-token": token}).json()["data"]
        self.assertNotIn("acl_devices", data["config"])

    # ---------------------------------------------------------- 播放链路
    def _seed_brief(self, client, provider="s_2"):
        title = cms.OnlineTitle(
            provider=provider, vod_id="acl-1", title="授权测试片",
            type_name="电影", year="2025",
            lines={"m3u8": [cms.Episode("正片",
                                        "https://a.invalid/acl.m3u8")]})
        brief = fake.brief_of(title, cms.TitleStore.guid_of(title))
        client.app.state.store.put_brief(brief)
        return brief

    def test_play_play_does_not_leak_unauthorized_source(self):
        self.enable(grants={"u1": {"libs": [], "sources": ["s_1"]}})
        upstream = {"code": -1, "msg": "official", "data": None}
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse(upstream))), \
                TestClient(appmod.app) as client:
            brief = self._seed_brief(client, provider="s_2")
            mg = fake.media_guid(brief["guid"])
            resp = client.post("/v/api/v1/play/play",
                               headers={"x-user-id": "u1"},
                               json={"media_guid": mg})
        self.assertEqual(resp.json(), upstream)
        self.assertNotIn("play_link", resp.text)

    def test_media_endpoint_does_not_require_identity(self):
        """播放器拉 m3u8/分片不一定带登录凭证，媒体端点只校验源是否启用。

        越权拦截在带凭证的入口（play/play、media/range）完成；这里保证不会
        因为「认不出是谁」把正在播放的链路打断。
        """
        self.enable(grants={"u1": {"libs": [], "sources": ["s_1"]}})
        with patch("hls.fetch_playlist",
                   AsyncMock(return_value=(
                       "#EXTM3U\n#EXTINF:4,\nseg1.ts\n", "https://a.invalid/x",
                       {"duration": 4}))), \
                TestClient(appmod.app) as client:
            brief = self._seed_brief(client, provider="s_2")
            mg = fake.media_guid(brief["guid"])
            resp = client.get(f"/v/media/online/{mg}/preset.m3u8",
                              headers={"x-user-id": "u1"})
        self.assertNotEqual(resp.status_code, 404)

    def test_authorized_account_can_play(self):
        self.enable(grants={"u1": {"libs": [], "sources": ["s_2"]}})
        with patch("app._passthrough",
                   AsyncMock(return_value=JSONResponse({"code": -1}))), \
                TestClient(appmod.app) as client:
            brief = self._seed_brief(client, provider="s_2")
            mg = fake.media_guid(brief["guid"])
            resp = client.post("/v/api/v1/play/play",
                               headers={"x-user-id": "u1"},
                               json={"media_guid": mg})
        body = resp.json()
        self.assertEqual(body.get("code"), 0)
        self.assertIn("/v/media/online/", body["data"]["play_link"])

    # ---------------------------------------------------------- 共享 store
    def test_store_does_not_downgrade_merged_lines_when_acl_on(self):
        store = cms.TitleStore()
        rich = brief_of(provider="s_1", lines={
            "L1": [{"url": "https://a.invalid/1.m3u8"}],
            "源二：L1": [{"url": "https://b.invalid/1.m3u8"}],
        }, mapping={"L1": "s_1", "源二：L1": "s_2"})
        poor = brief_of(provider="s_1")
        # 先关闭：旧行为是覆盖。
        store.put_brief(dict(rich))
        store.put_brief(dict(poor))
        self.assertEqual(store.get("g1")["line_names"], ["L1"])
        # 开启：不允许把多线路覆盖成单线路。
        self.enable(grants={"u1": {"libs": [], "sources": ["s_1"]}})
        store.put_brief(dict(rich))
        store.put_brief(dict(poor))
        self.assertEqual(store.get("g1")["line_names"], ["L1", "源二：L1"])


class OfficialSchemaTest(unittest.TestCase):
    """用官方库的真实表结构验证「自动读账号 + 自动识别身份」。

    官方布局：
    - ``trimmedia.db``   ``user(guid, username, is_admin, status)``
    - ``trimactivity.db`` ``user_token(token, user_guid, status, update_time)``
    上一版只认 ``uid``/``user_id`` 列，导致这两张表都识别不到，只能在管理页
    手工填账号；这一组用例就是防它回归。
    """

    def setUp(self):
        import sqlite3
        self._dir = tempfile.mkdtemp(prefix="fnmedia-acl-")
        med = sqlite3.connect(f"{self._dir}/trimmedia.db")
        med.executescript(
            "CREATE TABLE user (guid TEXT PRIMARY KEY, username TEXT,"
            " is_admin INTEGER, status INTEGER);"
            "INSERT INTO user VALUES"
            " ('u-guid-1', '爸爸', 1, 1),"
            " ('u-guid-2', '妈妈', 0, 1),"
            " ('u-guid-3', 'hyh_deleted_1773485874', 0, 1),"
            " ('default-user-template', '模板', 0, 1);"
        )
        med.commit()
        med.close()
        act = sqlite3.connect(f"{self._dir}/trimactivity.db")
        act.executescript(
            "CREATE TABLE user_token (id INTEGER PRIMARY KEY, token TEXT,"
            " user_guid TEXT, status INTEGER, update_time INTEGER);"
            "INSERT INTO user_token (token, user_guid, status, update_time)"
            " VALUES ('tok-dad', 'u-guid-1', 1, 10),"
            " ('tok-mom', 'u-guid-2', 1, 10),"
            " ('tok-old', 'u-guid-2', 0, 5);"
        )
        act.commit()
        act.close()
        self._cands = list(acl.borrow.CANDIDATES)
        acl.borrow.CANDIDATES = [self._dir]
        acl.discover(force=True)
        acl.invalidate()
        cfg = config.get()
        self._cfg = {"acl_enabled": cfg.get("acl_enabled"),
                     "acl_grants": dict(cfg.get("acl_grants") or {}),
                              "acl_subjects": list(cfg.get("acl_subjects") or [])}
        cfg.update({"acl_enabled": True,
                    "acl_grants": {"u-guid-1": {"libs": ["chart:movie"],
                                                "sources": []},
                                   "u-guid-2": {"libs": [], "sources": []}},
                    "acl_subjects": []})
        config.save(cfg)

    def tearDown(self):
        cfg = config.get()
        cfg.update(self._cfg)
        config.save(cfg)
        acl.borrow.CANDIDATES = self._cands
        acl.discover(force=True)
        acl.invalidate()
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_discovers_official_user_table(self):
        report = acl.discover(force=True)
        ids = [row["id"] for row in report["users"]]
        self.assertIn("u-guid-1", ids)
        self.assertIn("u-guid-2", ids)
        # 占位账号与该被删除的账号（官方改名为 xxx_deleted_<ts>）都不该出现。
        self.assertNotIn("default-user-template", ids)
        self.assertNotIn("u-guid-3", ids)
        # 管理员排最前，管理页第一眼就是自己。
        self.assertEqual(report["users"][0]["id"], "u-guid-1")
        self.assertTrue(report["users"][0]["is_admin"])
        names = {row["id"]: row["name"] for row in report["users"]}
        self.assertEqual(names["u-guid-2"], "妈妈")

    def test_session_lookup_uses_raw_authorization_token(self):
        ident = acl.identify(FakeRequest({"authorization": "tok-dad"}))
        self.assertEqual((ident.uid, ident.via), ("u-guid-1", "session"))
        self.assertEqual(ident.name, "爸爸")

    def test_session_lookup_accepts_bearer_prefix(self):
        ident = acl.identify(FakeRequest({"authorization": "Bearer tok-mom"}))
        self.assertEqual((ident.uid, ident.via), ("u-guid-2", "session"))

    def test_session_lookup_accepts_trim_mc_token_cookie(self):
        ident = acl.identify(FakeRequest({"cookie": "a=1; Trim-MC-token=tok-dad"}))
        self.assertEqual((ident.uid, ident.via), ("u-guid-1", "session"))

    def test_disabled_session_is_ignored(self):
        # status=0 的旧 token 不该把请求认成妈妈之外的账号；这里只验证
        # 「无有效 token 时不会命中已失效会话」。
        ident = acl.identify(FakeRequest({"authorization": "tok-old"}))
        self.assertFalse(ident.known)
        self.assertEqual(ident.reason, "no_mapping")

    def test_session_identity_gates_content_without_binding(self):
        ident = acl.identify(FakeRequest({"authorization": "tok-dad"}))
        token = acl.push(ident)
        try:
            self.assertTrue(acl.lib_allowed("chart:movie"))
            self.assertFalse(acl.lib_allowed("chart:tv"))
        finally:
            acl.reset(token)
        ident = acl.identify(FakeRequest({"authorization": "tok-mom"}))
        token = acl.push(ident)
        try:
            self.assertFalse(acl.lib_allowed("chart:movie"))
        finally:
            acl.reset(token)


if __name__ == "__main__":
    unittest.main()
