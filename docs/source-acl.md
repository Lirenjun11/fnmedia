# 按用户维度对已导入源做授权与管控 — 可行性与实施方案（定稿 v1.1）

> 分析对象：fnmedia-ext 0.0.1.1（本机 `D:/giteexm/fnmedia`），仅基于现有代码与文档。
> 约束：不编写实现代码，不引入现有技术栈之外的技术（Python/FastAPI、标准库、config.json、现有 React/Vite 管理页、官方 UDS 透传、官方 sqlite 只读）。
>
> **决策记录（2026-11-12 用户拍板，本版据此定稿）**
> - **D1 主体 = fnOS 官方账号**（不采用扩展自建成员）。
> - **D2 未显式授权 = 拒绝**（默认拒绝 / fail-closed）。
> - **D3 接受以 root 只读方式访问官方用户库**。

---

## 实施状态（0.0.1.13：对齐参考项目的「库可见 + 在线开关 + 默认放行」模型）

本文件下半部分是最初的可行性方案，**以下为实际实现与差异**，冲突时以本节为准。

| 项 | 实现 |
|---|---|
| 总开关 | `acl_enabled`，默认 `false`；关闭时全链路短路，行为与旧版一致 |
| 默认策略 | `acl_default_visible` / `acl_default_online`，**默认都为 `true`（放行）**：未单独配置的账号、以及暂时识别不出身份的客户端照常看得到内容，所以打开总开关不会让任何人瞬间失联 |
| 账号配置 | `acl_grants = {uid: {libs?: [key], sources?: [sid], online?: bool}}`。**键不存在 = 未单独配置（走默认）**；`libs: []` = 一个虚拟库都不给；`sources: []` = 一条在线片源都不给；`online: false` = 该账号禁用在线搜索与播放 |
| 主体清单 | **官方库自动读取**（`trimmedia.db` 的 `user` 表，含管理员标记；排除 `default-user-template` 与已删除账号 `*_deleted_*`）；`acl_subjects` 仅作手动兜底 |
| 身份来源 | ① 请求头（`x-user-id` 等 10 个候选）→ ② **官方登录 token 自动反查**（`Authorization` / `Trim-MC-token` → `trimactivity.db.user_token` → `user_guid`）|
| 拦截点 | 搜索源集、虚拟库列表/详情/条目（库根节点）、合并卡线路裁剪、详情/选集/播放四入口、`preset/main/direct` 媒体入口 |
| 可观测 | 管理页「当前身份」+ 默认策略 + 账号摘要（默认/已配 N 库/在线禁用）+ 自检接口（凭证候选、Cookie 键、官方库表发现、会话映射表）+ 分类日志 |
| 逃生通道 | 管理页与 `/v/api/ext/*` 永不参与 ACL；根用户改 `state/config.json` 的 `acl_enabled=false` 即恢复 |
| 导出 | 不含已废弃的 `acl_devices`；`acl_subjects` / `acl_grants` / 默认策略保留 |
| **未做** | **媒体 URL 票据化**：已下发的 m3u8 / 分片 URL 在授权撤销后仍可播放，直到缓存过期（阶段 2） |

**官方库位置与表结构**（只读，`mode=ro`；插件以 root 运行，权限足够）：

- 目录：`/usr/local/apps/@appdata/trim.media/database`，多卷回退 `/vol1`、`/vol2`。
- `trimmedia.db` → `user(guid, username, is_admin, status)`：账号清单与展示名。
- `trimactivity.db` → `user_token(token, user_guid, status, update_time)`：token 到账号的映射，取 `status=1` 且最新的一条。

**与参考项目（yujian0815/fnmedia-ext）的对齐情况**：数据模型与默认策略完全对齐 —— 它的 `lib_acl` / `acl_default_visible` / `acl_default_online` 对应这里的 `acl_grants[*].libs` / `acl_default_visible` / `acl_default_online`，语义一致（键不存在=未配置、空列表=都不给、默认放行）。**唯一的扩展**是允许把「在线」再细化到具体源（`sources` 非空时按源限制），参考项目只有账号级开关。

**两个已修的回归（0.0.1.12 / 0.0.1.13）**：

1. 0.0.1.11 只认 `uid` / `user_id` 列，官方用的是 `guid` / `user_guid`，导致账号自动读取与身份自动识别同时失效 → 现在按 `guid → user_guid → uid → user_id` 匹配，表名优先标准名。
2. 0.0.1.12 管理页总开关发的是 `acl_enabled`，后端只认 `enabled`，接口回 200 但配置没变，表现为「勾了又弹回」→ 现在后端两种字段都接受，前端以后端返回值为准并做失配提示。

**为什么没有「绑定设备」**：参考项目只靠官方 token 识别，本实现同样如此。设备绑定是早期识别失败时的临时兜底，0.0.1.15 已整段移除 —— 少一套需要维护的映射，也不会出现「绑定错了账号」这种自造问题。识别不出的客户端按默认策略处理（默认放行）。


**已知限制（UI 与文档同时说明）**：

1. 软管控：能改写请求头或拿到播放地址的技术用户可绕过；不做防作弊承诺。
2. 设备指纹基于登录相关 Cookie / Authorization 的哈希；清缓存、重新登录、换客户端后需要重新绑定。
3. 官方库表结构变化会让「会话反查」失效，此时可在管理页手动添加账号兜底。
4. **已解决（片源映射，见 [virtual-library.md](virtual-library.md)）**：路由仍全局一份，但改为按「源 × 分类」各建一条（`state/catalog.json` 的 `<sid>:<cat>`），虚拟库卡片按 `src:<sid>:<cat>` 生成；账号授权了哪些源就自动看到这些源对应的分类库（`provider_allowed`），仍可在库清单里逐个取消 `src:*` 键。按账号过滤只发生在出卡片时，不按账号建路由，避免缓存串号。
5. 播放状态（收藏 / 已看 / 续播）**已按账号隔离**（0.0.1.14）：身份解析与权限开关解耦，即使不开账号权限也会识别；旧全局记录由管理员首次访问时继承一次，其余账号从空白开始。

---

## 0. 结论（含决策后的变化）

**总体判断不变：有条件可行。软管控可实现；强管控需先解决身份绑定 + 媒体票据。但三项决策显著改变了风险结构。**

| 决策 | 带来的变化 | 净影响 |
|---|---|---|
| D3 root 只读官方用户库 | 身份绑定从「必须逆向官方 HTTP 会话校验接口」**降级为「本地只读查会话/用户表」**；已有 `borrow.py:41-53` 的同款只读连接模式可直接复用，`deploy/db_probe.py` 已有探测工具 | **可行性上升、逆向风险从中高降到中**；新增「官方库表结构稳定」这条依赖 |
| D1 主体 = fnOS 账号 | 授权主体可直接用官方 user_id，无需第二套账号体系；票据也能绑 user_id | 体验最自然；**会话映射仍然是唯一硬门槛** |
| D2 默认拒绝 | 身份解析失败不再是「降级放行」，而是**全员看不到在线源** | **可用性风险上升为最高项**，必须配可观测 + 逃生通道，否则就是用户最反感的「静默失败」 |

三条叠加后的**新增头号风险**：首次开启 ACL 且会话映射未验证通过时，所有账号（含管理员）的在线源会瞬间全部消失。方案第 8 章的「冷启动顺序」和「逃生通道」是硬性设计约束，不是可选项。

`README.md:14` 的「不做账号 ACL」、`docs/playback.md:15` 的「多账号共用一份状态」需同步修订（D1–D3 已视为对前者的批准）。

---

## 1. 需求含义与授权粒度

### 1.1 「已经导入的源」

- 存储：`state/config.json` 的 `sources[]`（唯一磁盘状态，`README.md:11`）。
- 身份：`subscription.source_id(api) = "s_" + sha1(api.strip().rstrip("/"))[:12]`（`subscription.py:67-68`），**由接口地址派生**，可直接作授权外键。
- 导入路径：订阅全量拉取 `admin_api.fetch_and_apply`（`admin_api.py:60-83`）、手动新增 `POST /v/api/ext/sources`（`admin_api.py:121-144`）。
- 全局开关：每条源 `enabled` + 全局 `enabled_limit`（默认 200，`config.py:129-133`）。

### 1.2 「使用」的 5 个动作与粒度

| 动作 | 入口 | 现有实现 |
|---|---|---|
| 搜索可见 | `GET /v/api/v1/search/list` | `app.py:272-304`，`config.enabled_sources()` 全量下发 |
| 详情/选集可见 | item/detail、season、episode、item/list | `app.py:307-435`，全局 `TitleStore` |
| 起播 | play/info、stream、play/play、media/range | `app.py:596-748` |
| 媒体读取 | preset/main/variant/direct/seg/poster/media/p | `app.py:772-1021`，**全部零鉴权** |
| 管理 | `/v/api/ext/*` | `admin_api.py`，`guard()` 单值 token |

- **主体（D1）**：fnOS 官方账号，主键用官方 user_id（不用用户名，改名不影响授权）。
- **推荐粒度**：主体 × 源 × `{search, play}`。**管理动作不参与按用户授权**（管理页是管理员入口，见 §6 逃生设计）。
- **不建议**：按影片/线路/分类授权。`cms.merge_titles`（`cms.py:209-228`）把同名影片跨源合成一张卡，线路只保留「源名：线路名」字符串前缀（`fake.py:125-145`），缺结构化的 `line → source_id` 映射；要支持就得先补这层元数据，成本与收益不成比例。

---

## 2. 现有权限模型

| 层面 | 现状 | 证据 |
|---|---|---|
| 管理写操作 | 单值 `admin_token`，`X-Admin-Token` 或 HttpOnly Cookie `fnos_media_admin` 任一匹配即放行；**不区分人** | `admin_api.py:19-26` |
| 管理页发 token | `GET /v/online-admin` 把同一个 token 写进 `<meta name="fnos-token">` 并 set-cookie，能打开该页的人都是管理员 | `app.py:247-262`、`web/src/api.ts` |
| 管理读接口 | `/bootstrap`、`/logs`、`/export` **连 guard 都没有**，返回完整 `sources[]`（含 `api`/`key`） | `admin_api.py:95, 362, 367` |
| 媒体 API | `/v/api/v1/*` 所有具名路由**零鉴权**；未命中虚拟条目就原样透传官方后端 | `app.py:272-1045` |
| 用户身份 | 无。`Authorization` / `Cookie` 仅被 `upstream._headers` 透传（`_HOP` 不排除这两项），扩展不解析、不校验、不落日志 | `upstream.py:8-13, 39-40` |
| 桌面网关 | `webui_gateway.py` 只重写路径，原样转发全部 header → **扩展后端能看到官方会话，只是没用** | `webui_gateway.py:26-41, 84-109` |
| 进程权限 | FPK `run-as: root`、`api-scope: []`；`borrow.py` 已用 root 只读打开官方 `trimmedia.db` | `packaging/fpk/config/privilege`、`resource`、`borrow.py:41-53` |
| 状态归属 | 收藏/已看/续播全局一份；`cms._CACHE`、`TitleStore`、hls 清单缓存、`_BAD_LINES` 全部进程级共享 | `playstate.py:11-18`、`cms.py:254`、`docs/playback.md:15` |

**一句话**：现在不是「权限太粗」，而是**根本没有「用户」这个概念**；`admin_token` 只是「管理页 vs 非管理页」的开关。

---

## 3. 授权数据来源（D1 + D3 后的定稿设计）

### 3.1 两条数据通道必须解耦（关键）

| 通道 | 用途 | 来源 | 失败后果 |
|---|---|---|---|
| **主体清单** | 管理页列出「有哪些 fnOS 账号可授权」，产出 `acl_subjects` | 官方 sqlite 用户表（**本地只读，D3**） | 清单为空 → 无法新增授权，但**已授权关系仍生效** |
| **请求绑定** | 判断「当前这次请求是谁」，产出 `subject_id` | 请求里的官方会话头 → 映射到 user_id（本地查会话/token 表为主，官方 HTTP 校验为辅） | 映射失败 → 按 D2 **拒绝**，在线源不可见 |

**解耦是 D2 下的硬要求**：即使当前会话解析不出来，管理员也必须能在管理页给任意账号授权、能关掉 ACL。如果两者耦合，一旦会话格式变化就会连「自救」都做不到。

### 3.2 为什么风险比 v1.0 低

v1.0 里 A 路径要求「向官方后端发一个 whoami 请求并解析返回」，接口名/字段全靠逆向（`docs/limitations.md:5` 已声明契约依据 `trim.media 0.9.8` 逆向、升级易碎）。D3 批准后，主路径变成**读本地 sqlite 会话/用户表做映射**：

- 已有同款实现：`borrow.py:48-53` 的 `mode=ro&immutable=0` + `timeout=2.0` + `check_same_thread=False`，以及 30s 进程内缓存（`borrow.py:25-27`）。
- 已有探测工具：`deploy/db_probe.py` 会打印官方库**所有表的列名、行数、样例行**，且该文件会打进 FPK（build.sh 的 `EXCLUDES` 未排除 `deploy/`）。
- 不产生额外外连，符合 `README.md:10`「零源纯透传、空闲 CPU ≈ 0」的约束。
- 官方 HTTP 校验接口降级为**可选交叉验证**，不再是必经之路。

### 3.3 仍然必须真机核验的 4 个假设（Go/No-Go）

1. 官方库里**存在**可映射到 fnOS 账号的表（用户表 + 会话/token 表），且列名稳定。
2. 请求头里的会话值**能在该表里查到 user_id**（`Cookie` 或 `Authorization`，具体字段待抓包）。
3. **媒体请求**（m3u8 / ts / mp4 Range，尤其手机 App 原生播放器、302 之后的分片请求）**也带同一会话头**。若不带，票据只能靠 URL 参数承载主体，阶段 2 的绑定强度要重新评估。
4. 三个候选库路径（`borrow.py:18-22`）在当前 fnOS 版本下有效，且 root 可读。

**注意**：代码里 `app.py:232`、`hls.py:111` 提到的「分享页」指的是**上游源站返回的分享播放页 HTML/JSON 里嵌的 m3u8**，不是本系统的对外分享功能。要核验的是「播放器发出的媒体请求是否带会话头」，不要被这个词误导。

### 3.4 读取边界（D3 的落地震界）

- 只读 `mode=ro`，**不写官方库**、不持有长连接、不建索引/触发器。
- 只取 user_id + 展示名；**token、密码哈希、手机号等一律不落盘、不进运行日志、不进 `export`**。
- 进程内缓存的 key 用会话标识的**哈希**，不存明文；TTL 与 `borrow.py` 一致（30s 量级），保证改密/登出/删号能较快生效。
- 官方库不可读或表结构变化 → 身份解析失败 → 按 D2 拒绝在线源，**但官方本地库影片照常**（走 `_passthrough`，不受影响）。
- 需在 README/免责声明里明示：本扩展以 root 只读访问飞牛影视数据库用于账号识别，属于超出原设计的数据访问范围。

---

## 4. 调用链路与拦截点

```
nginx /v/* ──> /var/run/trim_media.sock ──> uvicorn(app:app) ──> 官方 trim_media_upstream.sock
                    ▲ supervise.py 原子换名接管 (supervise.py:87-93)
桌面 iframe /app/fnmedia-ext/* ──> webui_gateway (UDS) ──> 同一 uvicorn /v/online-admin
```

| 编号 | 拦截点 | 现状 | 授权要做什么 |
|---|---|---|---|
| P1 | `search_list` → `config.enabled_sources()` (`app.py:275`) | 全量源 | 换成「当前 user_id 的授权源集 ∩ 全局 enabled」 |
| P2 | `cms.search_all` 的 `_CACHE[keyword]` (`cms.py:254-306`) | **按关键词全局缓存，跨用户串结果** | 缓存键带授权集指纹，否则 A 的结果直接漏给 B |
| P3 | `TitleStore` (`cms.py:323-387`) + 8 个详情/起播路由 | 全局 guid→brief，`lines` 混合多源 | brief 视图按主体裁剪线路；补 `line_sources` 结构化映射 |
| P4 | `preset/main/variant/direct/seg/poster` (`app.py:772-1002`) | **零鉴权**，`u=` 裸 base64，`Cache-Control: public, max-age=3600`（`hls.py:342`）、CORS `*` (`app.py:806`) | 统一票据校验，票据绑 user_id + 时效；去掉公共缓存、收紧 CORS |
| P5 | `admin_api` 源/订阅/导入导出 | 单值 token | 授权 CRUD、导出脱敏、订阅刷新不覆盖；**管理页自身不进 ACL** |

**最容易漏的一处**：`main.m3u8` / `preset.m3u8` / `variant` / `seg` 只靠 `mg` + `line` + `index` 就能从全局 `TitleStore` 还原播放地址，完全不经过 P1/P3。**只做搜索层过滤而不做 P4，等于没做。**

---

## 5. 影响范围

1. **数据层**：`config.DEFAULTS` / `save()` 是白名单式（`config.py:101-118`），新增字段**不进 DEFAULTS 会被静默丢弃**；`patch()` 同样忽略白名单外的键。
2. **订阅刷新会冲掉源上挂的字段**：`fetch_and_apply` 用 `normalize()` 产出的新对象整体替换 `sources[]`（`admin_api.py:73-81`），`merge_all`/`merge` 也按 id 覆盖（`subscription.py:138-162`）。**授权数据必须独立存键，绝不能挂在 source 元素上**，否则每 60s 的自动更新（`app.py:35-50`）会清空。
3. **导入导出**：`export_config` 会带出授权表，`import_config` 按白名单 update（`admin_api.py:367-384`）会用旧配置覆盖/清空授权 → 需要显式策略（脱敏导出 / import 保留现有授权）。
4. **全局共享状态**：`cms._CACHE`、`TitleStore`、`hls.get_playlist_text((guid,line,index))`、`hls._KEYS`、`_BAD_LINES`、`playstate` 全部跨用户共享，都要么带主体维度、要么在返回前过滤。
5. **官方本地条目**：所有「未命中虚拟条目」的分支走 `_passthrough` / `_passthrough_stream`，授权逻辑**不能触碰**。
6. **新增（D3）**：每个 `/v/api/v1/*` 请求都要做一次身份解析 → 必须有进程内缓存，不能每请求开 sqlite；查询失败要有明确分类（库不可读 / 表缺失 / 会话不匹配 / 会话过期），便于定位。
7. **新增（D2）**：`bootstrap`（`admin_api.py:95-110`）目前无条件返回全量 `sources`，按默认拒绝语义要裁剪；但**管理页的源管理视图必须仍看到全部源**（否则管理员无法授权），两者要分开。
8. **性能**：搜索 2.5s 总预算、起播 3s 并发（docs/playback.md:3）。按主体拆分缓存会增加内存与外连次数，需设上限。
9. **前端**：`Sources.tsx` 授权 UI、`Settings.tsx` 开关与主体清单、`api.ts` 类型、`App.tsx` 状态横幅。

---

## 6. 边界场景（D2 默认拒绝后重点）

| 场景 | 风险 | 处理（定稿） |
|---|---|---|
| **冷启动死锁** | 开启 ACL 瞬间无任何授权记录 → 含管理员在内所有账号在线源全消失；若会话映射也没验过，连自救都难 | **固定顺序**：① 先核验会话映射可用 → ② 读取主体清单 → ③ 在管理页先给管理员账号授权 → ④ 最后才置 `acl_enabled=true`。开启按钮在映射自检未通过时**置灰并说明原因** |
| **身份解析失败** | 按 D2 必须拒绝，但静默返回空结果正是最容易被误判为「插件坏了」的症状 | **可观测三件套**：运行日志写明失败分类；管理页顶部持久横幅「当前账号未识别，N 个在线源已隐藏」；提供只读状态接口供前端展示。绝不静默 |
| **逃生通道** | 管理页若因 ACL 故障不可用，无法关闭 | ① 管理页与 `/v/api/ext/*` **永不参与 ACL 判定**；② `acl_enabled` 只在管理页可改且改动立即生效；③ 文档写明 root 可直接编辑 `state/config.json` 把 `acl_enabled` 置回 `false`（唯一磁盘状态，改完由 `config.get()` 的 mtime 热加载生效，`config.py:90-98`） |
| **新账号 / 订阅新增源** | 默认拒绝 → 新用户看到零源、新源对所有人都不可见 | 符合 D2 预期，但必须**显式提示**：管理页显示「N 个账号未授权」「N 个新源待授权」，并提供批量授权 |
| **媒体请求无会话头** | 手机 App 原生播放器 / 302 后的分片请求可能不带 Cookie → 全链路 403，播放直接失败 | 阶段 0 第 3 项必须实测。若确实不带，则票据必须自包含 user_id 且短时效，P4 以票据为准、会话头为辅（见 §8.2） |
| **源删除后重新导入** | `source_id(api)` 不变，旧授权「复活」 | 删除时清理或标记孤儿授权，管理页给可见入口 |
| **`enabled_limit=200` 叠加** | 用户被授权但源全局未启用 → 看不到，难以解释 | UI 分开标注「全局启用」与「已授权」，判断取两者交集 |
| **成人源 `adult`** | 订阅拉入时默认禁用（`admin_api.py:71`） | 授权不能绕过 `adult` 过滤，两条件取交集 |
| **播放中撤销授权** | 已下发 play_link 仍可继续拉流 | 票据短时效 + 每次媒体请求校验；撤销后立即失效 |
| **官方登出/改密/删号** | 缓存里的旧映射继续放行 | 30s TTL + 票据短时效；不做账号生命周期同步 |
| **官方应用升级** | 表名/列名变化导致解析整体失败 | fail-closed + 明确日志 + 一键关闭；在 `docs/limitations.md` 登记该依赖 |
| **服务重启** | `TitleStore` / 搜索缓存全内存；票据密钥若也内存则所有票据失效 | 票据密钥持久化到 config.json；重启后虚拟卡片需重新搜索（既有已知限制） |
| **零源 / 纯透传** | 授权模块不应产生额外外连或 CPU | 所有授权判断只在命中虚拟条目时执行；官方库只读查询走缓存 |

---

## 7. 能否实现：明确结论、主要限制与风险

| 目标 | 结论 | 前置条件 |
|---|---|---|
| 按 fnOS 账号过滤「搜索/详情可见的源」 | ✅ 可实现，改动集中 | 阶段 0 四项核验全部通过 |
| 按 fnOS 账号拦截「起播入口」 | ✅ 可实现 | 同上 + brief 线路裁剪 |
| 防止「已知 URL 越权播放」 | ⚠️ 可实现但改动面最大 | 媒体票据化 + 全入口校验 + 缓存/CORS 调整 |
| 与 fnOS 账号体系完全对齐、跨客户端一致 | ⚠️ 不保证，取决于媒体请求是否带会话头 | 真机 Web + 手机 App 实测 |
| 播放状态按用户隔离 | ✅ 可实现，属独立改造 | 阶段 3，需迁移既有 `playstate` 数据 |

**三个硬限制（未变）**：① 身份绑定尚无实现、必须真机核验；② 媒体链路零鉴权、URL 可重放；③ 缓存/状态全局共享。

**按决策重排后的风险优先级**：
1. **D2 引发的可用性事故**（身份解析失败 → 全员无源）→ 用 §6 的冷启动顺序、可观测、逃生通道三件套压住。
2. **官方库契约脆弱**（D3 依赖表结构；`docs/limitations.md:5` 已说明官方升级需重新适配）→ 只读 + 缓存 + fail-closed + 文档登记。
3. **媒体请求无会话头**（决定票据设计）→ 阶段 0 实测，不实测就别进阶段 2。
4. **回归面广**：约 20 个具名路由 + 兜底透传、`cms.py` 缓存、`hls.py` 重写、管理 API、4 个前端页面；现有 116 项单测几乎无多用户语义覆盖。
5. **假安全**：只做 P1 不做 P2/P4，缓存串数据 + URL 可重放，比不做更危险。
6. **权限扩张**：root 读官方用户库（D3 已批准），但需在文档明示、且严格限定只读字段。

---

## 8. 可落地方案（定稿）

### 8.1 数据模型（全部落在现有 `state/config.json`）

| 键 | 内容 | 说明 |
|---|---|---|
| `acl_enabled` | bool，默认 `false` | 总开关；关闭时全链路短路 |
| `acl_subjects` | `[{id, name}]` | 来自官方用户表，只存 user_id + 展示名；作为授权 UI 的下拉项 |
| `acl_grants` | `{user_id: {source_id: {search, play}}}` | **独立键**，不挂 `sources[]` |
| `acl_ticket_secret` | 票据签名密钥 | 持久化，避免重启后票据全失效；导出时脱敏 |

必须全部加入 `config.DEFAULTS`；`_public_config()` / `bootstrap` 对非管理视图裁剪；`export` 脱敏 `acl_subjects` 与密钥；`import` 提供「保留现有授权」选项。

### 8.2 新增 `proxy/acl.py`（集中处理，鉴权不散落）

| 职责 | 说明 |
|---|---|
| `subjects()` | 只读官方 sqlite 拿用户清单（复用 `borrow.py` 连接模式，30s 缓存） |
| `subject_of(request)` | 从会话头 → 本地会话/token 表映射 user_id；可选官方 HTTP 交叉校验；失败返回带原因的「未识别」 |
| `allowed_sources(subject, action)` | `acl_grants` ∩ 全局 enabled ∩ 非 adult；**无记录 = 拒绝** |
| `filter_brief(brief, subject)` | 裁剪合并卡里的越权线路（依赖新增的 `line_sources` 映射） |
| 票据 | 标准库 `hmac` 签名，绑定 user_id + guid + line + 时效；签发在起播入口，校验在 8 个媒体 GET 入口；URL 参数承载，302 透传 |

### 8.3 改造点清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `config.py` | DEFAULTS 加 4 键；`save()` 对 acl 结构做校验与上限 |
| 2 | `proxy/acl.py`（新） | 上述 5 项职责 |
| 3 | `app.py:272` | `enabled_sources()` → `allowed_sources(subject, "search")`；透传分支不动 |
| 4 | `cms.py:254-306` | `_CACHE`/`_INFLIGHT` 键加授权集指纹；`merge_titles`/`brief_of` 保留 `line_sources` |
| 5 | `app.py:307-748` | 详情/选集/stream/play 统一走 `filter_brief`；`_pick`/`line_names` 基于裁剪视图 |
| 6 | `app.py:772-1002` | 8 个媒体 GET 统一票据校验；`rewrite_playlist` 的 `u=` 与 `play_link` 携带票据；`Cache-Control` 改 `no-store`；收紧 CORS |
| 7 | `hls.py:24-52, 342` | `encode_url/encode_seg` 支持票据参数（旧格式仅 `acl_enabled=false` 时有效，便于灰度）；移除 public 缓存 |
| 8 | `admin_api.py` | 授权 CRUD + 映射自检接口 + 状态接口；`bootstrap` 区分「管理视图全量」与「普通视图裁剪」；`fetch_and_apply` 不触碰 acl 键；`export` 脱敏；顺手给 `/bootstrap`、`/logs`、`/export` 补 `guard()` |
| 9 | `web/src/*` | `Sources.tsx` 授权列/弹层；`Settings.tsx` 开关 + 主体清单 + 批量授权；`App.tsx` 未识别/未授权横幅；`api.ts` 类型 |
| 10 | `README.md`、`docs/*` | 删除「不做账号 ACL」；记录默认拒绝、root 只读官方库、官方库表结构依赖、逃生方式 |

### 8.4 兼容性

- **默认关闭**：`acl_enabled=false` 时新增逻辑全部短路，行为与 0.0.1.1 完全等价；老配置缺字段由 `DEFAULTS` 兜底。
- **官方条目零影响**：ACL 只在命中虚拟条目的分支生效，`_passthrough*` 保持原样。
- **管理页豁免**：`/v/online-admin` 与 `/v/api/ext/*` 永不参与 ACL 判定，保证任何时候都能关闭。
- **灰度**：先只对 `search` 生效、`play` 全放行，观察一个版本再收紧；新旧 URL 格式并存一个版本，避免客户端缓存的旧 m3u8 立即 403。
- **fail-closed + 不静默**：解析失败一律拒绝在线源，同时写分类日志 + 前端横幅；`acl_enabled=false` 是最终逃生。
- **订阅刷新**：`acl_grants` 独立存储，刷新后原样保留；新增源按默认拒绝处理并提示。
- **数据迁移**：现有全局 `playstate` 保持不动，阶段 3 再隔离。

### 8.5 验证要点

- 单测（现有 116 项）新增：默认关闭时全链路等价；**未授权账号结果为空且不是静默空**（有状态返回）；两主体同一 keyword 结果不同（重点打 `_CACHE` 串数据）；未授权源不出现在 search/detail/season/episode/stream/play 任一响应；URL 重放 403；撤销后已下发 play_link 失效；票据过期/篡改/主体错配均拒绝；官方库不可读时 fail-closed 且官方本地条目仍可播。
- 缓存专项：`cms._CACHE`、`TitleStore`、hls 清单缓存、`_BAD_LINES` 的跨主体隔离。
- 链路专项：手机 App `media/range → 302 → main.m3u8 → seg` 票据不丢；AES-128 解密分片；mp4 Range 拖动。
- 数据专项：订阅自动刷新后授权不丢；源删除/重导入；`export → import` 往返不泄漏主体/密钥；`config.save` 白名单不吞字段；官方库表缺失/字段变化的降级。
- 官方回归：本地库影片搜索、播放、收藏、续播、真实文件 `media/range` 全走透传且不受影响。
- 性能：搜索 2.5s / 起播 3s 预算不退化；身份解析缓存命中率、按主体缓存膨胀上限。
- **真机**：≥2 个 fnOS 账号 × Web + 手机 App；逐项覆盖 §6 的冷启动、逃生、撤销、升级降级。

### 8.6 分阶段实施

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| **0. 真机核验（Go/No-Go）** | 见 §9，在 NAS 上只读探测 + 抓包 | **4 项假设全部通过才启动编码**；第 3 项不通过要重设计票据 |
| **1. 软管控（可发版）** | 主体清单 + 会话绑定 + 默认拒绝 + 冷启动顺序 + 可观测/逃生 + P1/P2/P3 + 授权 UI + 文档；默认关闭 | 单测全绿、真机两账号可见性正确、官方条目零回归。此时**明确标注不能防 URL 重放** |
| **2. 强管控** | P4 媒体票据化（绑 user_id）、缓存/CORS 收紧、撤销即时生效 | URL 重放 403、手机 App 全链路通过、性能预算不退化 |
| **3. 收尾** | `playstate` 按用户隔离、管理页只读/管理分级、README 设计约束正式修订 | 状态不串、迁移可回退 |

---

## 9. 阶段 0 核验清单（在 NAS 上执行，本机无法代做）

> 全部只读，不改动官方数据；探测输出可能包含 token / 密码哈希等敏感字段，**只在本地查看、不外传、用完删除**。

1. **拿官方库 schema**：找到扩展安装目录（`packaging/fpk/cmd/_common` 的 `REPO_DIR` 依次尝试 `${TRIM_APPDEST}/repo`、`${APP_ROOT}/target/repo`、`${APP_ROOT}/repo`），执行现成的只读探测脚本：
   `python3 <REPO_DIR>/deploy/db_probe.py > /tmp/fnmedia-db-probe.json`
   `deploy/db_probe.py` 会输出所有表的列名、行数与样例行；确认用户表、会话/token 表的真实名称与关联字段。
2. **确认会话映射**：用浏览器 DevTools 复制一次登录后 `/v/api/v1/search/list` 请求的 `Cookie` / `Authorization` 头，核对其中哪个值能在会话表里查到 user_id，以及是否可稳定区分不同账号。
3. **确认媒体请求带不带会话头**：抓手机 App 播放时的 `/v/media/online/.../main.m3u8`、`/v/media/online/seg?u=...`、mp4 Range 请求，以及 302 之后的后续请求，看是否携带与第 2 步相同的头。**这一项决定阶段 2 票据能否只靠 URL 自包含。**
4. **确认权限与路径**：三个候选库路径（`borrow.py:18-22`）哪个存在、root 是否可读；顺带记录当前 `trim.media` 版本（用于 `docs/limitations.md` 的依赖登记）。

**任一不通过的处置**：1/2 不通过 → 回到「官方 HTTP 会话校验」路径重新评估，或暂停立项；3 不通过 → 阶段 1 仍可做，阶段 2 票据必须自包含 user_id 并缩短时效；4 不通过 → 补路径探测，不改方案结构。

---

## 10. 剩余未决项（次要，不阻塞阶段 0）

1. `search` 与 `play` 是否真的需要分开授权，还是合并成一个「可用」开关（合并可让 UI 与数据结构都减半）。
2. 管理员账号是否默认拥有全部源（我倾向**否**，同样走显式授权，避免留下隐式后门）。
3. 授权撤销后，是否要同时清理该账号已有的收藏/续播记录（涉及阶段 3 的数据迁移语义）。
