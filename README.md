# fnmedia-ext

零侵入、零内置源、订阅驱动、带完整 Web 配置页的飞牛影视（`trim.media`）在线接入层。

> 配置之后才出现在线能力；停用或卸载，官方影视立即原样恢复。

## 关键约束

- **不内置任何源**：不附带资源站 / 订阅地址，包内只含协议与格式解析代码。
- **零源是合法默认状态**：干净安装后纯透传、空闲 CPU ≈ 0；除检查更新与日志页自动刷新外不产生额外外连，服务端没有任何常驻轮询。
- **磁盘状态可清理**：配置在 `state/config.json`；虚拟媒体库的三份缓存（榜单、分类路由、榜单绑定）均为可再生数据，可在管理页一键清空。
- **固定预算**：搜索 2.5s 总预算；起播 3s 并发优选最快线路，失败自动换线。
- **支持内网自建源**：默认允许订阅地址、海报、清单与直链指向局域网 / 回环地址（`allow_intranet`，可在「设置」关闭）；链路本地、保留与云元数据地址始终拦截。
- **不做**：常驻预热、后台自愈、全站测速、插件自更新。

## 安装

1. 在 fnOS 应用中心**手动安装 FPK**（`dist/fnmedia-ext-<version>.fpk`），需 fnOS 1.2.0+ 且应用「影视」正在运行。
2. 桌面出现「飞牛影视扩展」图标，此时为纯透传状态，官方影视一切照旧。
3. 打开管理页 →「订阅配置」粘贴你自己的订阅地址 →「拉取配置」全量导入并启用。
4. 需要定期同步时开启「自动更新」并设置间隔（默认 24 小时）。

管理页入口：桌面 iframe `/app/fnmedia-ext/`，或浏览器直连 `/v/online-admin`。

## 支持的订阅格式

| 格式 | 来源 |
|---|---|
| Base58 包装的 `api_site` JSON | Base58 订阅文本（含代理 `format=2`） |
| 明文 `api_site` JSON | 影视聚合工具配置文件 |
| `sources` 数组 JSON | `sources` 列表订阅 |
| TVBox `sites` | 通用 TVBox 配置（仅取苹果 CMS 采集站） |

订阅不可达时可在「配置内容」粘贴订阅文本离线导入；完整备份用「关于」页的导出 / 导入。

## 功能速览

| 功能 | 说明 | 详细文档 |
|---|---|---|
| 元数据清洗 + 官方刮削借用 | 简介分类清洗、背景图 / 评分 / IMDb，只读官方库 | [docs/metadata.md](docs/metadata.md) |
| 播放状态 | 收藏 / 已看 / 续播，按飞牛账号隔离，不写官方数据库 | [docs/playback.md](docs/playback.md) |
| 起播竞速与换线 | 3s 并发优选最快线路，失败自动切换 | [docs/playback.md](docs/playback.md) |
| 手机 App 播放兜底 | 修复手机端「文件权限不足」403 | [docs/playback.md](docs/playback.md) |
| 内网自建源 | 允许局域网 / 回环的订阅、海报与分片代理 | [docs/limitations.md](docs/limitations.md) |
| 检查更新红点 | 手动 + 打开页面自动检查，只读不安装 | [docs/update-check.md](docs/update-check.md) |
| 广告切片过滤 | 按清单文本特征跳过广告分片，默认关闭 | [docs/playback.md](docs/playback.md) |
| 虚拟媒体库 | 媒体库首页注入榜单四库 + 海量分类四库，可开关 | [docs/virtual-library.md](docs/virtual-library.md) |
| 账号权限 | 按 fnOS 账号控制可见的虚拟库与可用片源，默认关闭 | [docs/source-acl.md](docs/source-acl.md) |

## 开发

```bash
.venv/bin/python -m unittest discover -s tests -v   # 测试
bash packaging/fpk/build.sh --fnpack .tools/fnpack   # 打包 FPK
```

架构、构建细节与发版流程见 [docs/development.md](docs/development.md)；已知限制与免责声明见 [docs/limitations.md](docs/limitations.md)。

## 文档索引

- [docs/metadata.md](docs/metadata.md) —— 元数据清洗与官方刮削借用
- [docs/playback.md](docs/playback.md) —— 播放链路、播放状态、手机 App 兜底
- [docs/virtual-library.md](docs/virtual-library.md) —— 虚拟媒体库：榜单四库与海量分类四库
- [docs/source-acl.md](docs/source-acl.md) —— 账号权限：身份识别、库/源授权、逃生方式
- [docs/update-check.md](docs/update-check.md) —— 检查更新（只读）与红点提示
- [docs/development.md](docs/development.md) —— 架构、构建、测试、发版
- [docs/limitations.md](docs/limitations.md) —— 已知限制、免责声明、致谢
