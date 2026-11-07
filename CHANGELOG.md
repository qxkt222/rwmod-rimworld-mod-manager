# Changelog

## [0.8.0] - 2026-11-07

### Changed

- **前端全量重写为 React 19** — 15 个面板、Zustand 状态、自写 hash router、
  OpenAPI 自动生成 TS 类型、单一左侧栏 5 组导航、应用内弹窗替代原生 prompt/confirm
- **后端清债** — 安全（steamcmd 执行链 / 上传上限 / WS Origin / body 上限）、
  失败契约统一、多实例隔离、原子写、下载内核修复
- **拆包** — `downloader.py` 1122→642 行 + `download/`；`steamcmd.py` 667→392 行 +
  `steamcmd_support/`，历史 import 路径兼容
- **打包** — exe 82.5→51.9MB、安装包 121→70.7MB；版本单一来源；Docker 可复现
- **CSP** — HTML 文档响应加 `Content-Security-Policy`

### Added

- 前端测试体系：Vitest 31 例 + Playwright 82 条（真实后端、临时 HOME）
- CI Windows 打包 job 与 Windows pytest
- 标签管理 UI（创建 / 重命名 / 删除）
- 更新检查 `partial` 语义；合集预览区分不可达 / 空

### Removed

- 旧 Vanilla TypeScript 前端（24 个文件）
- `rwmod-web` 入口（执行即 TypeError）

## [0.7.0] - 2026-10-24

### Added

- **批量启停 Mod** — `POST /api/mods/active` 按 packageId 批量启用/停用，`GET /api/mods`
  增加 `active` 字段；前端 Mod 列表新增复选框与工具栏。写入大小写不敏感、只修改
  `activeMods`，落盘前生成 `.bak` 与 undo 快照，保留 CRLF/BOM（`modsconfig_edit.py`,
  `routers/mods.py`, `frontend/`）
- **Mod 设置管理** — 新增 `mod_settings.py` 与 `routers/mod_settings.py`：列出、查看、
  保存 `Config/Mod_*.xml`，支持单文件备份/恢复与「备份全部」。限制单文件 ≤ 2 MB、
  校验 XML well-formed、basename 白名单、写入前必须确认游戏已关闭（`ack_game_closed`），
  保留 BOM/CRLF；前端新增独立「Mod 设置」页（`panels/settings.ts`, `panel-registry.ts`,
  `layout.ts`）
- **Community Rules** — 接入 CC0 的 RimSort Community Rules Database，替换已失效的
  RimPy 上游 URL；`~/.rwmod/community_rules.json` 缓存 24h TTL、2 MB 上限、ETag/304、
  原子写、失败 fail-open（`rimpy_db.py`, `routers/rimsort.py`）
- **多实例支持** —
  - **Config v2**：新增 `[[instances]]` 与 `active_instance`；旧扁平配置加载时自动
    合成 `default` 实例，保存时保留 v1 兼容的扁平镜像（`config.py`）
  - **DB migration v3/v4**：`local_mod_metadata`、`mod_tags`、`download_queue`、
    `download_queue_flags` 按 `instance_id` 复合作用域重建；迁移前自动生成
    `.bak_v3` / `.bak_v4` 备份（`database.py`）
  - **队列按实例归属**：不同实例的同名 Mod 不混批，排队项记录所属实例且重启后保留；
    切换实例时若有 pending 下载返回 409（`queue.py`, `routers/config.py`）
  - **路径隔离**：profiles / undo 使用实例子目录，tags / cache 按实例作用域，
    transfer manifest 升级到 v2，saves 使用实例 config_dir（`profile.py`,
    `undo.py`, `tags.py`, `cache_db.py`, `transfer.py`, `routers/saves.py`）
  - **API / 前端**：实例 CRUD 与切换接口（`/api/config/instances`），配置面板新增
    实例卡片（`routers/config.py`, `frontend/src/panels/config.ts`）
- **Steam Workshop DB（可选）** — 接入 CC0 的 Steam Workshop 数据库（47 MB、
  57,679 条）；默认关闭，只有显式 `POST /api/rimsort/steamdb/refresh` 才会下载。
  缓存 `~/.rwmod/steam_db.json`：30 天 TTL、60 MB 上限、ETag/304、原子写、
  fail-open；`GET /api/rimsort/steamdb/status` 暴露缓存状态；`check-order` 可选附加
  gameVersions 信息（`steam_db.py`, `config.py`, `load_order.py`,
  `routers/rimsort.py`）
- **用户排序规则** — 新增 `~/.rwmod/rules.json`，优先级 用户 > 社区 > 内置；
  `sort_mods` 消费 `modDependencies`（438 条边）、`forceLoad*`（16+1）与版本化规则
  （v1.6）（`mod_rules.py`, `load_order.py`, `rimsort.py`）
- **排序诊断** — `check_load_order` 新增「声明规则 vs 实际顺序」检查；真实 522 项配置
  0 未解析、0 循环，跨分区违例显式报告（`load_order.py`）
- **Mod 大小快照** — 新增 `mod_sizes.py`：后台 `scandir` 快照、TTL 10 分钟、写入后
  可失效；560 个真实 Mod 后台扫描 3.63 秒，不阻塞请求（`mod_sizes.py`,
  `routers/dashboard.py`, `routers/mods.py`）
- **健康检查持久缓存** — 新增 `mod_health_check.py`：`mod_cache` 持久缓存 6 小时、
  single-flight 刷新、允许读取 stale 值；网络失败不再把 Mod 误标为 removed
  （`mod_health_check.py`, `routers/health.py`）
- **队列重试 / 暂停 / 恢复** — 新增 `POST /api/queue/retry`、`/api/queue/pause`、
  `/api/queue/resume`，前端队列面板对应按钮（`routers/queue.py`, `queue.py`,
  `frontend/`）

### Changed

- **SteamCMD 执行模型** — 增加全局运行锁：真正的 SteamCMD 下载串行执行，安装校验与
  合集探测可并行；配合 5/15/45 秒（±20% jitter）退避重试（`steamcmd.py`,
  `downloader.py`）
- **下载错误透出** — 新增结构化 `DownloadOutcome` / `BatchResult`（保留
  `dict[str, bool]` 旧契约）；失败原因经队列、WebSocket、SSE、前端与下载历史展示，
  不再只返回一个 false（`downloader.py`, `queue.py`, `routers/download.py`,
  `frontend/`）
- **取消语义隔离** — 取消的项保持 `CANCELLED`，使用 completed 集合避免重复处理；
  被取消批次的未完成兄弟项重新排队，取消标记在结束后清理（`downloader.py`,
  `queue.py`）
- **更新流程加固** — force 更新前持久备份（默认开启，`backup_keep=3`）、安装后校验、
  失败时按 stash → backup 顺序回滚旧版（`downloader.py`, `backup.py`）
- **Skymods 默认关闭** — `Config.enable_skymods` 默认 `False`；首次失败后打开 10 分钟
  熔断，避免对 Cloudflare 403 反复重试刷日志；`/api/status` 暴露 `sources.skymods`
  （disabled / blocked）（`config.py`, `skymods.py`, `routers/health.py`）
- **响应性能（553 Mod 沙箱实测）** — `/api/mods` 热缓存 11 ms（旧 5.2–8.6 s）、
  `/api/dashboard` 6–8 ms（旧 25 s）、`/api/mods/health` 11 ms（旧 14 s）
- **supported_versions 持久化** — SQLite migration v2 增加
  `local_mod_metadata.supported_versions`；旧缓存行按 miss 重新解析一次后回填，
  冷/热缓存结果一致（`database.py`, `cache_db.py`, `metadata.py`）
- **packageId 大小写归一** — 全链路按大小写不敏感比较；`compare` 的缺失项从 420
  降到 5（仅 DLC）；存档解析同样大小写不敏感并复用 mod_cache，真实存档 missing
  从 420 降到 6（Core + 5 DLC）（`metadata.py`, `mod_rules.py`, `save_parser.py`,
  `routers/saves.py`）
- **官方内容锚定** — load order 固定 Harmony → Core → Royalty → Ideology → Biotech →
  Anomaly → Odyssey；`generate` 使用真实游戏版本与 knownExpansions
  （`load_order.py`, `rimsort.py`, `profile.py`）
- **富格式依赖** — 支持 `<modDependencies>`、`alternativePackageIds` 与 DLC 豁免；
  510 组依赖解析，真正缺失为 0（`mod_rules.py`, `load_order.py`）
- **ModsConfig 路径显式化** — 新增 `Config.config_dir`、
  `profile.resolve_modsconfig_path()` / `modsconfig_target()`；apply / profile restore /
  undo / CLI restore 全部传入显式 config_dir，不再隐式扫描 LocalLow（`config.py`,
  `profile.py`, `modsconfig_edit.py`, `routers/profiles.py`, `routers/undo.py`,
  `cli.py`）
- **测试沙箱守卫** — `RWMOD_TEST_SANDBOX_HOME` 在解析符号链接后仍越界时 fail-closed，
  抛出 `SandboxViolationError`；该变量只用于测试隔离，不是生产开关（`profile.py`,
  `tests/conftest.py`）
- **既有未发布改动（10/18）** — 队列重启恢复、force 更新意图持久化、批量覆盖 stash
  回滚、循环依赖防护、SteamCMD 非 ASCII 路径预检、下载历史入库（`queue.py`,
  `downloader.py`, `steamcmd.py`, `database.py`）
- **清理** — 删除 10/18 产生的 102 个 `assert_steamcmd.exe_*.dmp`（约 17 MB，
  用户授权）（`dumps/`）

### Fixed

- **批量删除路径穿越** — 批量删除接口此前仅拦截 `/`、`\` 与 `..`，Windows 驱动器相对
  路径（如 `C:Windows`）与 NTFS 数据流名（`mod:stream`）不含分隔符却会绕过检查；
  现仅接受纯文件夹名，并校验解析后的路径仍位于 mods_dir 内（`routers/mods.py`）
- **下载失败被旧内容掩盖** — 显式失败（如 `Get details for item ... failed`）不再因
  上次下载残留的文件而被判成功并刷新 `last_updated`；仅当日志没有该 Mod 的结论
  （UNKNOWN）时才信任磁盘内容（`steamcmd.py`）
- **安装版 SteamCMD 目录不可写** — 安装到 Program Files 后普通用户无法在
  `{app}\steamcmd` 下创建 `logs/`、`steamapps/`、`appcache/`，导致默认安装下所有
  下载失败；安装时为该目录授予 Users 修改权限（`rwmod.iss`）
- **Docker 镜像启动失败** — `useradd -r` 未创建家目录，`Path.home()/.rwmod.db`
  不可写；现设置 `HOME=/app/data` 并先 mkdir/chown 再声明 VOLUME（此前顺序导致卷为
  root:root）；compose 去掉 SteamCMD 的 `:ro` 挂载并把配置卷改挂 `/app/data`
  （`Dockerfile`, `docker-compose.yml`）
- **合集 ID 不再交给 SteamCMD** — 合集探测失败时明确失败，而不是把合集 ID 本身当作
  Mod 下载（此前触发 `no About.xml` → Skymods 403 的失败链）（`downloader.py`）
- **健康检查误标 removed** — 网络失败与「Mod 已下架」区分开，网络错误保留旧状态
  （`mod_health_check.py`）
- **ModsConfig.xml 被误写事故（如实记录）** —
  - **时间**：2026-10-24 15:47:28。
  - **原因**：S 组独立验证脚本调用 `rimsort_apply`；`resolve_modsconfig_path()` 优先
    扫描 LocalLow，脚本未 patch `Path.home()`；同秒幂等调用覆盖了唯一备份。
  - **影响**：真实 `Config/ModsConfig.xml` 的 activeMods 顺序被改写；未丢失 Mod，
    未改动数据库 / 日志 / 配置 / `D:/RimWorld/`。
  - **恢复**：用 Player.log（10/18 13:32）的 522 项加事故前记录的最后 10 项重建；
    当前 522 个 unique 项，保留 version/knownExpansions，XML 合法、无 BOM、CRLF；
    前 12 与后 10 项与事故前记录一致，尾部 12 个位置与 Player.log 不同；
    `sakurata.ideotickopt`、`gwinnbleidd.mothballedanddeadpawns.zh` 原位无法确定，
    暂放 510/511。当前 sha256
    `054251852cf5e71b814e64cab5c6ed514fa61434c2353a0645fb12b91ef92136`。
  - **证据保留**：`Config/ModsConfig.xml.rwmod-incident-sorted.bak`、
    `Config/ModsConfig.xml.bak_20261024_074728`、
    `~/.rwmod/undo/20261024_074728_sort.xml`。
  - **根因修复**：见上节「ModsConfig 路径显式化」与「测试沙箱守卫」；新增 20 个
    `tests/test_config_path_safety.py` 用例。
  - **未完成**：仍需用户进游戏确认最终排序；两个 Mod 的原始位置无法从现有证据精确
    恢复。

### Tests

- 离线 pytest：**1382 passed, 0 xfailed, 12 deselected**（升级前 952 passed /
  35 xfailed）
- `run_tests.py` 六项门禁 **6/6 通过**；`ruff format --check src/ tests/` 96 个文件通过
- 新增测试：`test_mod_sizes.py`、`test_mod_health_check.py`、
  `test_modsconfig_edit.py`、`test_mod_settings.py`、
  `test_config_path_safety.py`（20 个安全用例）
- 原有 33 个 xfail 已全部转正：PARSE-2..7、DATA-1..9、SEC-1..4、API-1/2/4/7、
  DL-6/7/9 等

## [0.6.0] - 2026-09-13

### ⚠️ 破坏性变更
- **移除登录鉴权** — 项目定位为个人本机 / 局域网自用，删除 JWT 认证与密钥管理：
  删除 `src/rwmod/auth.py`、`src/rwmod/routers/auth.py`、`frontend/src/auth.ts`，
  各业务路由的 `Depends(get_current_user)` 全部摘除，`/ws` 不再走子协议鉴权，
  移除 `pyjwt` 依赖；前端登录弹窗与 token 注入删除，API 对同网段客户端开放
  （`server.py`, `deps.py`, `routers/*`, `frontend/`, `pyproject.toml`, `uv.lock`）

### ✨ 新功能
- **SteamCMD 批量下载** — 由「每 Mod 一个 SteamCMD 进程」改为「每批一个进程」
  （`BATCH_SIZE=6`），单进程内连续 `+workshop_download_item`，配合批量元数据扫描
  与按层批量依赖查询，显著降低大合集下载开销（`steamcmd.py`, `downloader.py`,
  `queue.py`, `database.py`）
- **实时下载进度** — 解析 SteamCMD stdout 的 `Update state ... progress` 行，经
  SSE / WebSocket 推送；前端下载与合集面板新增进度条与速率显示
  （`steamcmd.py`, `routers/download.py`, `queue.py`, `frontend/`）
- **Skymods 流式落盘** — 备用源下载由整包读入内存改为 1 MiB 分块流式写盘，
  边写边校验大小上限，大包不再占满内存（`skymods.py`）

### 🧪 测试
- 新增 `tests/test_batch.py`（批量下载 / 批量 SteamCMD / 进度回调）；同步更新
  `test_steamcmd.py`、`test_skymods.py`、`test_security_batch.py` 至无鉴权模型
- 离线测试套件现为 **267 通过**

### 📦 打包 / 清理
- `rwmod.spec` 移除指向已删除模块的 `rwmod.routers.auth` hiddenimport；
  `.pre-commit-config.yaml` 移除 `types-PyJWT`
- README / RELEASE_NOTES 同步「已移除鉴权」，并更正测试数与版本口径

## [0.5.0] - 2026-08-07

### 安全加固（面向公开发布 + 局域网部署）
- **上传大小限制强制生效** — 修复 `UploadFile.size` 为 `None`（chunked 上传）时
  大小检查被跳过、整个请求体被读入内存的问题：所有上传端点改为按字节硬上限读取
  （`utils.read_upload_limited`）；`.rwmod` 导入改为流式写盘 + 计数截断，多 GB 包
  不再占满内存（`routers/transfer.py`, `routers/mods.py`, `routers/download.py`）
- **配置路径校验** — `/api/config` 拒绝相对路径、磁盘根目录、程序运行目录与
  Windows 系统目录（`C:\Windows` 等），防止备份/删除接口触碰系统路径
  （`routers/config.py`）
- **`.rwmod` 导出默认脱敏** — Steam API 密钥不再随包导出（默认只记录"是否有密钥"，
  手动勾选"包含密钥"才会写入，用于自机迁移）；前端配置面板新增该开关
  （`transfer.py`, `frontend/`）
- **WS token 改走子协议** — `/ws` 认证令牌从 `?token=` 查询参数迁移到
  `Sec-WebSocket-Protocol`（`rwmod.<token>`），避免令牌进入访问日志/浏览器历史；
  旧 `?token=` 形式仍兼容（`server.py`, `frontend/src/ws.ts`）

### 行为改进
- **force 覆盖原子化** — 覆盖安装改为"旧版 rename 暂存 → 下载成功再删旧"，下载
  失败自动回滚，不再有"先删后下、失败即丢 mod"的窗口（`downloader.py`）
- **取消下载真正终止 SteamCMD** — 队列取消会终止正在运行的 SteamCMD 进程
  （Windows 发送 CTRL_BREAK 信号覆盖子进程），不再是"标记取消但后台继续跑"
  （`steamcmd.py`, `queue.py`）

### 架构 / 数据
- **SQLite 迁移框架** — `PRAGMA user_version` 版本化迁移，老数据库原地升级不丢数据
  （`database.py`）
- **更新时间戳入库** — mod 的 `.rwmod_last_updated` 标记文件迁入
  `local_mod_metadata.last_updated`，不再污染游戏目录、兼容只读挂载；老标记文件
  自动迁移并清理（`database.py`, `workshop.py`, `downloader.py`, `cache_db.py`）
- **DB 访问收敛** — `_get_conn` 公开为 `get_conn`，移除未使用的 `get_db` 依赖
- **配置修正** — `steamcmd_path` 尊重用户配置的合法路径，仅当配置路径失效时回退内置
  副本（`config.py`）
- **清理** — 移除 no-op 的 `_try_broadcast` 死代码、`profile._safe_filename` 重复助手；
  合集页面爬取复用共享 HTTP 连接（`workshop.py`）

### 测试
- 新增批次测试：上传限读、配置路径校验、`.rwmod` 脱敏、WS 子协议认证、
  force 覆盖回滚、SteamCMD 取消、schema 迁移、`last_updated` 保留与旧标记迁移
  （`tests/test_security_batch.py`, `tests/test_downloader.py`, `tests/test_steamcmd.py`,
  `tests/test_database.py`, `tests/test_workshop.py`, `tests/test_config.py`）
- 离线测试套件：**254 通过**；ruff / mypy（strict）全绿

## [0.4.5] - 2026-08-05

### 安全 (P1) — 代码审查专项修复
- **全新鉴权模型** — 取消公开默认密钥（`rwmod-dev-secret` 列入禁用名单）。本机
  （localhost）访问免密钥直接可用；仅局域网客户端需密钥（密钥持久化于
  `~/.rwmod.secret`，启动日志打印）。设置强 `RWMOD_SECRET` 可启用严格模式。
  登录采用恒定时间比较 + 失败限速（`auth.py`, `routers/auth.py`）
- **认证失效根因** — `get_current_user` 缺 `return user`，`/api/auth/verify` 返回
  `user: null`；已修复（`auth.py`）
- **workshop_id 校验链** — `PublishedFileId.txt`/URL 参数的 workshop_id 全部强制
  `^\d+$`，堵住备份 zip 任意写盘（`backup.py`）、restore 目录穿越
  （`routers/backups.py`）、SteamCMD 命令注入（`steamcmd.py`/`parser.py`）三条路径
- **Skymods SSRF** — 下载 URL 白名单域名 + 拒绝私网/回环 IP + 响应大小上限（`skymods.py`）
- **zip 炸弹防护** — `safe_extract_zip` 成员数/解压总量/重复路径上限（`utils.py`）；
  5 个上传端点统一大小限制（`mods.py`/`transfer.py`/`rimsort.py`/`saves.py`）
- **鉴权收紧** — `/ws`（需 `?token=`）、`/api/onboarding/check`、`/api/metrics` 需认证

### 稳定性 / 并发 (P2)
- **下载锁死锁** — per-mod 锁的依赖/合集子项改为锁外收集后下载，环形依赖不再互等
  （`downloader.py`）
- **worker 崩溃修复** — `_worker_loop` 异常兜底，一次 OSError 不再杀死队列
  （`queue.py`）
- **SQLite 死锁** — `init_db` 重入 `_init_lock` 导致启动卡死；`close_db` 后线程
  拿已关闭连接报错；均修复（`database.py`）
- **SSE 心跳 + 断线语义** — 30s 心跳防反代掐断；合集 500+ 子项改一次性目录映射
  （`routers/download.py`）
- **WebSocket 背压 + 任务引用** — 慢客户端超积压断开；广播任务不再被 GC
  （`server.py`）
- **事件循环阻塞** — dashboard/import-local/`_find_existing` 移入 `to_thread`；
  mods 缓存 TTL 3s→30s（`routers/mods.py`, `routers/dashboard.py`）
- **原子性** — 备份恢复 rename 交换+回滚（`backup.py`）；profile/rimsort 原子写
  （`profile.py`, `routers/rimsort.py`）；force 失败自动回滚（`downloader.py`）
- **transfer 导出** — 唯一临时文件名 + 响应后清理（`routers/transfer.py`）
- **`_find_existing` 快路径** — 返回值类型不匹配导致每次回退全目录扫描，已修复

### 前端
- **认证流程重写** — 删除硬编码默认密钥；本机免登录直进；401 统一重登
- **SSE 超时兜底 / 面板补齐**（Saves/Tags/RimSort/History）/ **WS 轮询兜底** /
  **interval 清理** / **undefined 修复** / **XSS 转义**

## [0.4.4] - 2026-08-02

### 修复 (P1)
- **前端认证缺失导致所有面板 401** — 0.4.3 后端强制启用 JWT 认证，但前端从未实现
  登录/token 机制，导致所有 `/api` 请求返回 401，首页显示 `undefined`/`NaN GB`。
  现新增前端自动登录 + 登录界面兜底，并在启动时统一为所有 `/api` 请求注入
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
