# 检查更新（只读）

打开管理页时自动检查一次（也可在「关于」页手动刷新），后端访问 GitHub `releases/latest`，只展示最新正式版版本号、更新说明与 Release 页面链接：

- 只认 `vX.Y.Z` 正式版，跳过草稿与预发布；5 秒超时、成功缓存 6 小时 / 失败缓存 10 分钟（纯内存），失败静默不打扰
- 发现新版本时「关于」导航项显示常亮红点、关于页版本号旁显示 NEW 标记；红点不因查看页面而清除，直到检查结果不再有新版本
- 不做后台轮询、不下载 FPK、不安装、不替换程序文件；升级请在 fnOS 应用中心手动安装（`cmd/upgrade_init` / `cmd/upgrade_callback` 会保留 `state/config.json`）
- 默认仓库为 `Lirenjun11/fnmedia`；仓库地址可在配置 `update_repo` 修改，仅接受 `owner/repo` 形式并拒绝路径穿越
- `release.yml` 在仓库自身运行，tag 与 `VERSION` 一致时自动打包并把 FPK / sha256 发布到该仓库的 Release，检查更新会直接指向这里
- 版本单一来源仍为 `VERSION`，`release.yml` 校验 tag 与版本一致并上传 `fpk + sha256`
