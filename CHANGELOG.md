# Changelog

## [0.4.4] - 2026-08-02

### 修复 (P1)
- **前端认证缺失导致所有面板 401** — 0.4.3 后端强制启用 JWT 认证，但前端从未实现
  登录/token 机制，导致所有 `/api` 请求返回 401，首页显示 `undefined`/`NaN GB`。
  现新增前端自动登录（默认密钥 `rwmod-dev-secret`）+ 登录界面兜底（用户修改
  `RWMOD_SECRET` 时手动输入密码），并在启动时统一为所有 `/api` 请求注入
  `Authorization: Bearer <token>`（`frontend/src/auth.ts`、`frontend/src/main.ts`）

## [0.4.3] - 2026-08-02


### 新功能
- **一键迁移** (`transfer.py`) — 将配置档案、备份、标签与设置打包为 `.rwmod` 文件，
  可跨机器一键导出/导入（`routers/transfer.py`，前端配置面板「一键迁移」）
- **操作撤销** (`undo.py`) — 破坏性操作（排序/恢复）前自动快照 `ModsConfig.xml`，
  可一键回滚（`routers/undo.py`，前端配置档案面板「操作撤销」）

### 安全 (P1)
- **认证默认失效修复** — `get_current_user` 在未配置密钥时直接放行，导致默认配置下
  所有 API 完全无鉴权。现改为强制启用认证（`auth.py`），测试 `client` fixture 自动携带 token

### 稳定性 / 功能 (P2)
- **SSE 实时推送卡死修复** — `/api/events` 原为无限循环且无心跳，连接永久挂起；
  改为带心跳的 `asyncio` 事件循环（`server.py`）
- **WebSocket 队列推送链路打通** — 队列状态变化通过 `/ws` 实时广播到前端
- **Profile 重命名崩溃修复** — 用 `Path.stem` 解析文件名导致带 `.xml` 后缀的路径
  解析错误（`profile.py`）
- **磁盘统计低估修复** — dashboard 只统计 Mods 目录本身，未递归计算子目录大小；
  改为递归统计（`routers/dashboard.py`）
- **SQLite 写并发锁** — 多线程写库可能触发 `database is locked`，加写锁保护（`database.py`）
- **备份恢复原子性** — 恢复时先写临时目录再原子替换，中途失败不再丢失数据（`backup.py`）
- **依赖 BFS 与健康检查误判** — 依赖遍历改用 BFS 保证顺序；健康检查不再把 Core 误判为缺失
- **Core 位置校验 / offline 探测 / CORS / 合集路由** — 多处边界问题修复
- **Docker 只读挂载修复** — Mods 目录挂载为只读导致无法写入（`Dockerfile`）
- **ModsConfig 对比误报** — `compare_modsconfig` 把内置 Core 误算为 extra，已排除（`rimsort.py`）

### 稳定性 / 功能 (P2) — 代码复查补充
- **下载队列并发竞态修复** — 原 `start()` 在 `_running` 为 True 时直接返回，
  队列运行期间新加入的待下载项会被静默丢弃；改为单一持久 worker 任务 +
  `asyncio.Event` 唤醒，运行中新增项也能被拾取（`queue.py`）
- **WebSocket 广播解阻塞** — 广播改为 `asyncio.gather` 并发发送，慢/卡死的
  客户端不再阻塞整个事件循环的队列通知（`server.py`）
- **备份恢复目录匹配修复** — `restore_mod` 原按备份文件名解析的（可能被
  `safe_filename` 改写过的）目录名定位解压目录，遇到含 `:`/`?` 等非法字符的
  mod 目录会恢复失败；改为从 zip 内容检测实际顶层目录（`backup.py`）
- **Skymods 重定向递归限制** — `_download_and_extract` 对 HTML 重定向页无限
  递归，遇到 A→B→A 循环会栈溢出；加 `_MAX_REDIRECTS=5` 深度上限（`skymods.py`）

### 清理 (P3)
- **测试断言过时** — `run_tests.py` 版本断言更新至 0.4.3
- **mypy 清理** — `transfer.py`/`autoupdate.py` 类型注解补全，0 错误
- **ruff format** — 全量格式化 23 个文件
- **dead code 清理** — 删除 `backup.py` 中未使用的 `_safe_filename` 包装函数
  与 `list_backups` 冗余分支
- **测试扩充** — 新增 `restore_mod` 目录名被改写场景测试，全套 223 项测试通过

## [0.4.2] - 2026-08-01


### Fixed
- **合集下载彻底修复** — `is_collection`/`fetch_collection_children`/
  `fetch_item_dependencies` 之前用 POST 调用 `IPublishedFileService/QueryFiles`，
  Steam 返回 405 导致合集从未被识别、依赖检测从未生效。现改用专用端点
  `ISteamRemoteStorage/GetCollectionDetails`（返回完整子项列表）与
  `GetPublishedFileDetails`（依赖检测）（`workshop.py`）
- **合集 ID 不再遗漏** — 拉取结果改为「API + 精确 HTML 爬取」双源合并（并集），
  任一来源出现的子项都不丢；HTML 爬取改用精确的 `sharedfile_<id>` 模式，
  不再混入侧栏相关推荐噪音（已验证 API=爬取=页面 childCount=593）
- **合集预览接口异常兜底 + 性能优化** — 后端 try/except 返回错误信息而非 500；
  原来 593 个子项每个都全目录扫描 + 查一次数据库，改为一次元数据扫描 +
  一次历史查询（`routers/workshop.py`）
- **前端预览不再显示 "undefined"** — `previewCollection` 先检查 HTTP 状态，
  失败时显示后端 detail（`panels/collection.ts`）
- **测试扩充** — workshop 新增 14 项 mock 测试（合集检测/子项解析/双源合并/
  依赖解析），全套 222 项通过

## [0.4.1] - 2026-08-01

### 安全 (P1)
- **修复备份接口路径穿越** — `delete_backup`/`restore_mod` 现在只接受纯文件名
  （拒绝 `../`、`..\`、子目录），zip 解压前校验成员路径，无法再删除/写入
  备份目录以外的任意文件（`backup.py`, `routers/backups.py`, `utils.safe_extract_zip`）
- **修复 SteamCMD 超时失效** — 改用 `proc.communicate(timeout=...)`，卡死的
  SteamCMD 进程会在 10 分钟后被真正终止，不再永久挂起（`steamcmd.py`）
- **Skymods 解压同样启用 zip 路径穿越防护**（`skymods.py`）

### 稳定性 / 功能 (P2)
- **异步端点不再阻塞事件循环** — `/api/download`、`/api/import/*`、
  `/api/download/stream`（合集分支）统一用 `asyncio.to_thread` 执行 SteamCMD，
  下载期间其他 API 请求不再无响应（`routers/download.py`, `autoupdate.py`）
- **WebSocket 实时队列广播** — 队列状态变化通过 `/ws` 推送到前端，
  队列面板不再需要手动刷新（`server.py`, `queue.py`）
- **移除首页自动触发更新检查** — 打开首页不再自动联网检查/覆盖下载，
  仅由「一键更新」按钮显式触发（`routers/dashboard.py`）
- **队列取消语义** — 取消下载中的项目后状态保持 cancelled，不再被
  后台任务改回 done/failed（`queue.py`）
- **SQLite 并发** — 连接增加 `PRAGMA busy_timeout=5000`，降低多线程写库冲突
- **config API 脱敏** — 不再向前端返回 `steam_api_key`，改为 `has_steam_api_key`
  （`routers/config.py`, 前端 `api.ts`）
- **`Config.validate()` 放宽** — 仅要求 SteamCMD 存在；RimWorld 游戏目录为可选

### 清理 (P3)
- **前端接入 hash 路由** — 支持 `#saves`/`#tags` 深链接与浏览器前进/后退；
  删除死代码 `store.ts`/`ui.ts`（`router.ts`, `main.ts`, `cmd.ts`）
- **缓存隔离** — mods/dashboard 缓存按 `mods_dir` 区分，切换目录不再串数据
- **tools 脚本清理** — 移除硬编码 `D:\` 路径与 UTF-8 乱码，改为相对路径
- **README 架构图** — 路由数修正为 17 个，快速开始注明 bun/npm 均可
- **测试扩充** — 新增 skymods/rimsort/队列取消/备份穿越/SteamCMD 超时等 43 项测试
  （覆盖率 49% → 56%）
- **CI** — bandit 使用 `-ll` 作为硬性门禁

## [0.4.0] - 2026-07-21

### Added
- **合集 3 层降级拉取** — Steam Web API → HTML 抓取 → 用户 API Key
- **存档解析** (`save_parser.py`) — 从 RimWorld save 文件读取 mod 列表
- **Mod 标签** (`tags.py`) — 本地标签分组管理
- **优化** — 批量请求并发、Steam 连接复用

### Fixed
- **CLI `import-collection` 崩溃** — `workshop_download()` 返回 `DownloadResult`，
  旧代码按元组解包导致 `TypeError`，现已改为使用 `output_lines`/`success`
- **CLI `list_mods` 类型注解** — 4 元组 (folder, name, pkg, wid) 注解修正
- **`search_workshop` URL 编码** — 使用不存在的 `urllib.request.quote` → `urllib.parse.quote`
- **`downloader._try_broadcast` 引用不存在的 `server.broadcast_queue_update`**
  — 改为显式 no-op 钩子，消除静默失败路径
- **mypy 清理** — 163 → 0 错误（routers 按 FastAPI 惯例豁免 no-untyped-def；
  业务模块补全注解；`type-arg` 统一豁免）

## [0.3.0] - 2026-07-17

### Added
- **持久化 Mod 元数据缓存** (`cache_db.py`) — SQLite 存储 About.xml 解析结果，重启后无需重新扫描全部 Mod
- **更新前自动备份** (`backup.py`) — 覆盖 Mod 前自动 zip 旧版，支持一键回滚
- **更新日志 / Changelog** — 检查更新时展示 Steam Workshop 的 file_description
- **依赖关系预览** — 下载面板输入 Mod ID 时自动显示依赖树（✓已装/⚠缺失）
- **版本兼容性检测** (`compatibility.py`) — 对比 RimWorld 版本与 Mod 的 supportedVersions
- **Mod 配置档案** (`profile.py`) — 保存/恢复 ModsConfig.xml 快照，PC/PS 端切换
- **Steam API 连接复用 + 并发** — 共享 urllib opener + ThreadPoolExecutor 并行批量请求
- **Steam 合集导出** — 从已安装 Mod 反向生成 Workshop ID 列表
- **排序分析** (`load_order.py`) — 检测 Harmony 位置、Core/DLC 顺序、已知冲突、重复项
- **离线模式** (`offline.py`) — Steam API 不可达时用本地缓存降级
- **CLI 新增命令**: `profile-save/list/restore/delete`, `backup-list/restore/cleanup`, `compat`, `check-order`

### Fixed
- **一键更新无效** — `autoupdate.py` 未传 `force=True`，导致已安装 Mod 被跳过
- **更新面板 ID 不匹配** — `updates.ts` 用了不存在的元素 ID，检查结果永不显示
- **Mod 列表空壳** — `#mod-list` 未渲染实际内容
- **chcp 报错** — 删除了 `.bat` 中的 `chcp 65001`，避免受限 shell 环境报错

### Changed
- `check_mod_updates()` — 复用 `get_cached_mods()` 替代重复的 XML 解析
- `get_cached_mods()` — `os.scandir()` 替代 `Path.iterdir()`（2-20x 提速）
- `downloader.py` — 移除无用的下载后备份代码，改为覆盖前调用 `backup.py`

## [0.1.0] - 2025

### Initial Release
- SteamCMD 集成：匿名下载、批量导入
- Workshop 搜索（Steam Web API）
- 合集下载（Web API + SteamCMD 双路径）
- ModsConfig.xml 导入/对比（RimSort 兼容）
- 下载队列、SSE 日志流
- Web UI dashboard
