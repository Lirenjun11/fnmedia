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
  playstate.py     播放状态存取（收藏 / 已看 / 续播）
  admin_api.py     管理 REST 接口 + 订阅全量拉取与定时刷新
  webui_gateway.py 桌面 iframe 网关
  upcheck.py       GitHub 只读检查更新
  static/          Web 配置页构建产物（单 JS + 单 CSS）
web/               Vite + React + TypeScript 源码
deploy/            systemd 单元与安装 / 卸载 / 依赖脚本
packaging/fpk/     FPK 骨架与 build.sh
```

请求链：`nginx /v → /var/run/trim_media.sock → uvicorn`；具名路由命中在线条目时伪造响应，其余请求原样转发 `trim_media_upstream.sock`。管理页走独立桌面网关，不进入媒体链路。

管理页打开时按需检查一次更新（命中 6 小时缓存则不出网）；「日志诊断」页打开期间前端每 2 秒刷新一次日志，离开页面即停止——服务端没有任何常驻轮询。

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
