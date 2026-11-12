# 开发与架构

## 架构

```
proxy/
  app.py           FastAPI 主应用：具名路由 + 兜底透传
  supervise.py     UDS 原子换名接管、健康检查、退出回滚
  upstream.py      官方后端 Unix Socket 透传（JSON / 流式）
  config.py        唯一磁盘状态与热加载
  subscription.py  订阅解析、归一化、SSRF 防护
  netguard.py      订阅 / 海报 / 分片 / 直链的统一目标校验（内网放行策略）
  cms.py           苹果 CMS 搜索与内存标题库
  fake.py          官方响应伪造与 TV 端严格字段规整
  hls.py           m3u8 改写、分片与海报代理
  playstate.py     播放状态存取（收藏 / 已看 / 续播），按飞牛账号分区
  charts.py        豆瓣榜单缓存（虚拟库展示层）
  catalog.py       资源站分类深翻（虚拟库内容层）
  vlib.py          虚拟媒体库定义、卡片、榜单条目懒绑定
  vlib_api.py      mediadb/list、mdb/{guid}、mediadb/sum 接管
  acl.py           账号权限：身份识别、库/源授权、合并卡线路裁剪
  admin_api.py     管理 REST 接口 + 订阅全量拉取与定时刷新
  webui_gateway.py 桌面 iframe 网关
  upcheck.py       GitHub 只读检查更新
  static/          Web 配置页构建产物（单 JS + 单 CSS）
web/               Vite + React + TypeScript 源码
deploy/            systemd 单元与安装 / 卸载 / 依赖脚本
packaging/fpk/     FPK 骨架与 build.sh
```

请求链：`nginx /v → /var/run/trim_media.sock → uvicorn`；具名路由命中在线条目时伪造响应，其余请求原样转发 `trim_media_upstream.sock`。管理页走独立桌面网关，不进入媒体链路。

搜索接口在扩展侧做会话级输入防抖（`search_debounce_ms`，默认 200ms，`0` 关闭，上限 1000）：飞牛客户端录入即搜产生的连续请求仍各自透传官方后端，但按会话只有最后一次会去查在线源，中间请求只返回官方本地结果；防抖等待不计入 `search_timeout` 预算，预算从防抖结束、开始查询时才起算。会话按凭据哈希分组，避免多设备互相抑制。飞牛前端的请求时机与官方本地结果均不受影响。

管理页打开时按需检查一次更新（命中 6 小时缓存则不出网）；「日志诊断」页打开期间前端每 2 秒刷新一次日志，离开页面即停止——服务端没有任何常驻轮询。

账号权限集中在 `acl.py`：HTTP middleware 每个请求解析一次身份（请求头 → 官方登录 token 在 `trimactivity.db.user_token` 反查 `user_guid` → 管理页设备绑定兜底，结果缓存 30s），授权判定只读 `acl_grants`。**身份解析与权限开关是解耦的**：即使没开 `acl_enabled` 也会识别，`playstate` 靠它做按账号隔离；请求头那一路不进指纹缓存（指纹不含请求头，缓存会让同机不同账号串号）。账号清单直接读 `trimmedia.db` 的 `user` 表（含管理员标记），不需要手工录入。所有详情 / 选集 / 播放链路都经由 `_brief()` → `acl.filter_brief()`，按线路的真实来源（`line_sources`）裁剪合并卡；虚拟库列表与详情按 `chart:*` / `catalog:*` 键过滤。`/v/api/ext/*` 与 `/v/online-admin` 永不参与判定，保证权限配置出问题时仍能在管理页关闭。

虚拟媒体库的后台任务只在缓存过期时刷新（启动 5 秒后检查一次，之后每 10 分钟判断，命中 `vlib_refresh_hours` 才真正出网）；媒体库列表永远读本地缓存，刷新不阻塞页面。详见 [virtual-library.md](virtual-library.md)。

管理 API 写操作校验管理页下发的令牌（HttpOnly Cookie 或 `X-Admin-Token` 请求头），令牌不出现在 URL 与访问日志中。

## 构建

```bash
# 前端（产物需复制到 proxy/static）
cd web && npm install && npm run build
cp -a dist/. ../proxy/static/

# FPK（需要 fnpack，macOS 版会放在 .tools/fnpack）
bash packaging/fpk/build.sh --fnpack .tools/fnpack

# 可选：不含离线 Python wheelhouse 的精简包（安装时需联网装依赖）
bash packaging/fpk/build.sh --slim --fnpack .tools/fnpack
```

`wheelhouse/` 提供离线 Python 依赖（fastapi / uvicorn / httpx），是 FPK 体积的主要来源；`--slim` 会去掉它，改由 `deploy/bootstrap_deps.sh` 在线安装。

应用图标由 `tools/generate_icon.py` 用纯标准库绘制（深墨青圆角方 + 薄荷绿几何 F + 接入圆环），不复用任何第三方或旧项目素材。运行 `python3 tools/generate_icon.py` 会同时生成 FPK 根目录图标、桌面 `app/ui/images/icon_*.png`，以及管理页左上角使用的 `web/public/app-icon.png`。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 发版

版本号单一来源为 `VERSION`（FPK 文件名、manifest、前后端兜底展示均由它派生，另有少量兜底字符串需同步）。发布流程：提交 → `git tag vX.Y.Z` → `git push origin vX.Y.Z`，`release.yml` 校验 tag 与 `VERSION` 一致后打包上传 `fpk + sha256` 并创建 GitHub Release。不要强推已发布的 tag。
