# JS 源（drpy2 单文件规则）

「片源配置 → 添加 JS 源」可以直接粘贴 TVBox 生态里的 drpy2 单文件源（`var rule = {...}` 的 `.js` 全文）。后台只做两件事：

1. 把 `rule` 这个**对象字面量**解析成 Python dict；
2. 用 [selectolax](https://github.com/rushter/selectolax) 执行规则里的 HTML/CSS 选择器。

不引入 Node、不内嵌 JS 引擎，因此安装包只多约 1.5 MB（双架构 wheel 都在 `wheelhouse/`）。

## 规则键与实现

| 规则键 | 用法 |
|---|---|
| `host` / `headers` / `timeout` | 站点根地址、请求头（`MOBILE_UA` 会展开成移动端 UA）、超时（毫秒，钳到 4–20 秒） |
| `class_parse` | `选择器;名称;链接;正则`，正则第 1 组或链接本身就是分类 id |
| `url` | 分类分页模板；`fyclass` / `fypage` / `fyfilter` 是占位符，`[首页地址]` 表示第 1 页用另一个地址 |
| `一级` / `推荐` | `列表选择器;标题;图片;备注;链接`，`*` 表示「同一级」 |
| `二级` | `{title, img, desc, content, tabs, lists}`，每项可用 `;` 写多条兜底 |
| `searchUrl` | `路径#表单体`，`**` 是关键词，结尾 `;post` 表示 POST；实际入口不是 `index.php` 时自动再试 `so.php` |
| `搜索` | 搜索结果列表，规则同 `一级` |
| `lazy` / `play_parse` | **不执行**（见下） |

选择器支持可串联的 `&&`、取属性（`Text` / `Html` / `href` / `src` / `data-*`），以及 drpy 常用的 jQuery 扩展伪类 `:eq(n)`、`:gt(n)`、`:lt(n)`、`:first`、`:last`、`:contains(文本)`、`:not(:contains(文本))`。

## 播放地址怎么来

`lazy` 里是任意 JS，没有引擎跑不了。播放改走内置的通用解析：起播时抓一次播放页，按顺序尝试

1. MacCMS 的 `player_data` / `player_aaaa` JSON：`encrypt=0` 明文、`1` 转义、`2` Base64 + 百分号解码；
2. 页面里直接出现的 `m3u8` / `mp4` 地址（Aliplayer 的 `source: "..."`、`<video src>` 等）；
3. 只有第三方 `<iframe>` 时判定失败，日志记 `drpy play: iframe only`。

解析结果按 URL 缓存 6 小时，并写回条目——同一集只在第一次起播时多一次请求。

## 已知边界

- **磁力线路放不了**：`magnet:` / `ed2k:` / `thunder:` 会被跳过（播放器放不了磁力链，要离线下载 + 转码，不在本扩展范围内）。纯磁力源能浏览、能看详情，但会提示没有可播线路。
- **`js:` 代码块**：`二级.tabs` / `二级.lists` 写成 `js:` 时无法求值，退化到「结构化兜底」——把 `.context` 下每个含播放链接的 `.widget` 当成一条线路。6v 这类 MacCMS 多线路主题实测可用。
- **只有 iframe 的线路**：详情阶段会用第 1 集探一次，探不出来（需要浏览器嗅探）的线路直接剔除，避免点到必然失败的线路。
- **规则里的分类过滤**：`filter`（二级筛选）暂不支持，只取 `class_parse` 的主分类。

## 实测过的源

| 源 | 分类 | 列表 | 详情/集数 | 搜索 | 播放 |
|---|---|---|---|---|---|
| `dbku.js`（独播库，MacCMS + mytheme） | 8 | 48/页 | 8 集 | 2 条 | `player_data` → m3u8 |
| `6v影视.js`（6V，磁力站） | 14 | 18/页 | 多线路 | 2 条 | 4 条线里 2 条给 `source:` 直链 m3u8，2 条是第三方 iframe（已剔除） |

两个源都可以用 `tests/test_drpy.py` 里的离线用例覆盖解析逻辑；真实站点可用下面的脚本自查（不落库）：

```
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'proxy'); import drpy; \
print(drpy.parse_rule(open('dbku.js',encoding='utf-8').read())['title'])"
```

## 相关代码

- `proxy/drpy.py`：规则解析、选择器、列表/详情/搜索、播放页解析
- `proxy/catalog.py`：分类、分页
- `proxy/cms.py`：搜索分发
- `proxy/app.py`：`_online_brief`（补线路）与 `_drpy_play_url`（起播前换直链）
- `proxy/admin_api.py`：`POST /v/api/ext/sources/drpy`
