# rwmod — RimWorld Mod Manager 项目深度调查报告

> 调查时间：2026-08（基于仓库当前状态，`git log` 最新提交 7439949）
> 调查方式：全部结论来自仓库内一手来源（源码 / README / CHANGELOG / RELEASE_NOTES / pyproject.toml / git 历史 / 测试代码 / 前端源码 / 构建脚本），未使用外部资料。
> 报告内所有引用均为「相对路径:行号」或「相对路径（文件名）」。

## 摘要

- rwmod 是一个**全离线 RimWorld Mod 管理器**（v0.5.0，MIT）：用 SteamCMD 匿名下载创意工坊 Mod，提供 Web UI + CLI 双入口，无需 Steam 客户端。
- 后端为 FastAPI + SQLite（Python ≥3.13），前端为零运行时依赖的 Vanilla TypeScript（Vite 8 构建）；后端约 7.2k 行、前端约 3.4k 行（实测行数统计）。
- 核心能力：下载（SteamCMD + Skymods 双源）、合集下载、依赖管理、自动更新、备份回滚、配置档案、排序分析、存档解析、标签、一键迁移、操作撤销。
- 安全模型经过多轮专项加固（0.4.1 → 0.5.0）：JWT 鉴权（本机免密钥 / 局域网需密钥）、workshop_id 数字校验链、zip 路径穿越与炸弹防护、Skymods SSRF 防护、上传硬上限、配置路径黑名单。
- 质量门禁齐全：254 项测试、ruff / mypy strict / bandit / pre-commit / CI（7 个 job）。
- 分发形态：PyInstaller 单文件 EXE + Inno Setup 安装包（内置 SteamCMD），另有 Docker 三阶段镜像。
- 主要风险：`run_tests.py` 版本断言已过时（仍断言 0.4.5）；2 个上传端点仍存在 chunked 上传未限读的残留缺口；`app_state.py` 是半成品抽象。

---

## 1. 项目概述

- **定位**：全离线 RimWorld Mod 管理器 — 无需 Steam 客户端即可匿名下载、更新、备份与管理 Mod（README.md:6）。
- **版本**：0.5.0（`pyproject.toml:3`、`src/rwmod/__init__.py:3`、`src/rwmod/server.py:105`、`rwmod.iss:3`）。
- **作者/仓库**：qxkt222，GitHub `qxkt222/rwmod-rimworld-mod-manager`（`pyproject.toml:39-40`）。
- **许可证**：MIT（`LICENSE`，Copyright (c) 2026 qxkt222）。
- **语言**：后端 Python ≥3.13（`.python-version`、`pyproject.toml:7`）；前端 TypeScript 5.7（`frontend/package.json:12`）。
- **开发状态**：Beta（classifier `Development Status :: 4 - Beta`，`pyproject.toml:13`）。
- **功能清单**（README.md:10-25）：SteamCMD 匿名下载 + Skymods 备用源、Workshop 搜索（Web API 免 Key）、合集批量下载、一键更新、实时队列（WebSocket）、依赖管理、备份回滚、健康检查、兼容检测、排序分析、配置档案、合集导出、一键迁移（.rwmod 包）、操作撤销。
- **合规声明**（README.md:162-171）：SteamCMD 匿名下载仅限个人备份自用；内置 SteamCMD 版权归 Valve；Skymods 仅作备用源；与 Ludeon/Valve 无关联。

---

## 2. 技术栈

| 层 | 技术 | 依据 |
|---|---|---|
| 后端框架 | FastAPI ≥0.100 + uvicorn ≥0.30（含 websockets） | `pyproject.toml:24-26,28-29` |
| CLI | Typer ≥0.25 | `pyproject.toml:26`；入口 `rwmod.cli:app`（`pyproject.toml:35`） |
| 数据校验 | Pydantic ≥2.0（仅少量模型） | `pyproject.toml:27`、`src/rwmod/models/schemas.py` |
| Web 鉴权 | PyJWT ≥2.8（HS256） | `pyproject.toml:30`、`src/rwmod/auth.py:125` |
| XML 安全 | defusedxml ≥0.7（XXE 防护） | `pyproject.toml:31`、`src/rwmod/xmlutil.py:15` |
| 存储 | SQLite（WAL、每线程连接池、`PRAGMA user_version` 迁移） | `src/rwmod/database.py:60-102,137-211` |
| 实时推送 | WebSocket（`/ws`）+ SSE（`/api/download/stream`） | `server.py:241`、`routers/download.py:142` |
| 可观测 | Prometheus 文本格式 `/api/metrics`（自研，非 prometheus-client 库） | `routers/metrics.py:62-139` |
| 前端 | Vanilla TypeScript + Vite 8（rolldown 构建） + esbuild；**零运行时 JS 依赖** | `frontend/package.json:10-14`、`frontend/vite.config.ts` |
| 网络 | urllib 共享 opener + ThreadPoolExecutor 并发批次 | `workshop.py:26-27,342-361` |
| 打包 | PyInstaller 6 + Inno Setup 6.7（`iscc_out.txt`） | `rwmod.spec`、`rwmod.iss` |
| 容器 | Docker 三阶段构建（python:3.13-slim + node:22-alpine） | `Dockerfile:7-51` |
| 质量 | pytest+cov / ruff / mypy(strict) / bandit / radon / pre-commit / codespell | `pyproject.toml:46-114`、`.pre-commit-config.yaml` |

依赖面很小：运行时仅 fastapi/uvicorn/typer/pydantic/python-multipart/websockets/pyjwt/defusedxml（`pyproject.toml:23-32`）。

---

## 3. 架构与模块职责

### 3.1 目录树（源码部分，非构建产物）

```
rwmod/
├── src/rwmod/
│   ├── server.py          # FastAPI app factory：中间件/WS/静态资源/错误处理
│   ├── deps.py            # 依赖注入：Config / Queue / AutoUpdateManager 单例
│   ├── errors.py          # 统一异常体系 RwmodError → HTTP 状态码
│   ├── auth.py            # JWT 签发/校验/密钥管理（~/.rwmod.secret）
│   ├── config.py          # ~/.rwmod.toml 读写 + 2s TTL 缓存
│   ├── database.py        # SQLite 持久层（5 张表 + 迁移框架）
│   ├── cache_db.py        # Mod 元数据增量缓存（按 mtime）
│   ├── mod_cache.py       # 元数据三层缓存（内存→SQLite→全量扫描）
│   ├── metadata.py        # About.xml + PublishedFileId.txt 解析（ModMeta）
│   ├── xmlutil.py         # defusedxml 安全解析封装
│   ├── workshop.py        # Steam Web API：搜索/合集/依赖/更新检测
│   ├── steamcmd.py        # SteamCMD 子进程封装 + 错误分类 + 进程终止
│   ├── downloader.py      # 下载编排：重试/Skymods 回退/force 原子覆盖/依赖 BFS
│   ├── queue.py           # 下载队列（并发 3 + SQLite 持久化 + WS 通知）
│   ├── skymods.py         # Skymods 备用源（SSRF 防护 + zip 校验）
│   ├── backup.py          # 更新前自动备份 / 原子恢复 / 清理
│   ├── transfer.py        # .rwmod 一键导出/导入（含密钥脱敏）
│   ├── undo.py            # ModsConfig.xml 操作撤销快照
│   ├── profile.py         # Mod 配置档案（ModsConfig.xml 快照）
│   ├── rimsort.py         # RimSort 集成：生成/对比/拓扑排序
│   ├── load_order.py      # 排序分析（Harmony/Core/DLC/已知冲突）
│   ├── compat.py          # 依赖缺失与互斥检测（modDependencies/incompatibleWith）
│   ├── compatibility.py   # RimWorld 版本兼容检测
│   ├── save_parser.py     # .rws 存档解析（正则，50MB+ 不整读）
│   ├── parser.py          # modlist/合集目录/ModsConfig 解析 + workshop_id 解析
│   ├── rimpy_db.py        # RimPy 社区冲突数据库（下载+24h 缓存）
│   ├── tags.py            # Mod 标签（SQLite）
│   ├── offline.py         # Steam API 连通性状态 + 降级
│   ├── autoupdate.py      # 自动更新管理器（24h 后台检查 + 手动触发）
│   ├── i18n.py            # 双语内置词典（zh-CN/en，仅导航文案）
│   ├── logger.py          # ~/.rwmod.log 滚动日志（5MB×3）
│   ├── cli.py             # Typer CLI（13 个命令）
│   ├── utils.py           # extract_mod_id / safe_filename / safe_extract_zip / read_upload_limited
│   ├── app_state.py       # （半成品）AppState 容器
│   ├── models/schemas.py  # 少量 Pydantic 模型
│   └── routers/           # 20 个路由模块（见 3.2）
├── frontend/              # Vite + TS 前端（src/main.ts + src/panels/*）
├── static/                # 前端构建产物（server 挂载）
├── steamcmd/              # 内置 SteamCMD（本地 1.86GB，未纳入 git）
├── tests/                 # 254 项测试（19 个文件）
├── tools/                 # build_fe.py / build_frontend.py / fix_lint.py
├── .github/workflows/ci.yml
├── pyproject.toml / justfile / Dockerfile / docker-compose.yml
├── rwmod.spec / rwmod.iss / start.bat / start_no_build.bat
├── run_tests.py / verify_fixes.py / schemathesis.toml
├── README.md / CHANGELOG.md / RELEASE_NOTES.md / SECURITY.md / LICENSE
└── docs/agents/           # agent 协作约定（domain.md / issue-tracker.md）
```

### 3.2 路由模块（19 个，全部 `/api` 前缀，均在 `server.py:194-213` 注册；与 README.md:137 一致）

| 模块 | 职责 | 关键文件 |
|---|---|---|
| routers/auth.py | 登录（限速）/ token 校验 | `routers/auth.py` |
| routers/health.py | 连通状态 / locale / onboarding | `routers/health.py` |
| routers/config.py | 配置读写（含路径校验黑名单） | `routers/config.py` |
| routers/dashboard.py | 首页统计 / SteamCMD 检测 | `routers/dashboard.py` |
| routers/mods.py | Mod 列表/健康/兼容/导出/本地导入/批量删除下载/汉化检测 | `routers/mods.py` |
| routers/download.py | 下载 / SSE 流 / 导入（file/collection/sort） | `routers/download.py` |
| routers/workshop.py | 搜索 / 详情 / 合集预览 / 依赖查询 | `routers/workshop.py` |
| routers/queue.py | 队列增/启/删/清 | `routers/queue.py` |
| routers/backups.py | 备份列表/恢复/删除/清理 | `routers/backups.py` |
| routers/compat.py | 依赖缺失+冲突检查 | `routers/compat.py` |
| routers/profiles.py | 配置档案 CRUD + 恢复前快照 | `routers/profiles.py` |
| routers/rimsort.py | 生成/对比/排序分析/应用排序 | `routers/rimsort.py` |
| routers/history.py | 下载历史/统计/清空 | `routers/history.py` |
| routers/auto_update.py | 自动更新检查/状态/结果 | `routers/auto_update.py` |
| routers/metrics.py | Prometheus 指标（含请求埋点） | `routers/metrics.py` |
| routers/tags.py | 标签 CRUD | `routers/tags.py` |
| routers/saves.py | 存档列表/详情/上传分析/补下载 | `routers/saves.py` |
| routers/transfer.py | .rwmod 导出/导入 | `routers/transfer.py` |
| routers/undo.py | 撤销快照列表/回滚 | `routers/undo.py` |

### 3.3 核心业务模块职责表

| 模块 | 行数 | 职责 |
|---|---|---|
| workshop.py | 369 | Steam Web API 客户端：搜索 / GetCollectionDetails 合集 / GetPublishedFileDetails 批量详情与依赖 / 更新检测 / HTML 爬取 |
| downloader.py | 355 | 下载编排：per-mod 锁、3 次重试、合集检测、依赖 BFS、force 原子覆盖、Skymods 回退 |
| queue.py | 311 | 下载队列：并发信号量、持久 worker、SQLite 持久化、WS 通知、取消杀进程 |
| steamcmd.py | 274 | SteamCMD 子进程：错误分类（workshop_log 解析）、超时终止、cancel_download 进程注册表 |
| backup.py | 244 | 备份 zip / 原子恢复 / 清理（保留 N 份） |
| skymods.py | 207 | Skymods 备用源：搜索页解析、SSRF 防护、zip 校验解压 |
| rimsort.py | 251 | RimSort 集成：生成/对比 ModsConfig、拓扑排序 |
| transfer.py | 184 | .rwmod 导出/导入（密钥脱敏、流式导入） |
| parser.py | 159 | modlist / 合集目录（bin+JSON）/ ModsConfig / workshop_id 解析 |
| profile.py | 156 | ModsConfig.xml 档案快照（原子写） |
| rimpy_db.py | 148 | RimPy 社区冲突数据库（下载 + 24h 缓存） |
| database.py | 341 | SQLite 持久层 + 迁移框架 |
| cli.py | 332 | Typer CLI 13 命令 |
| server.py | 297 | FastAPI app factory |

### 3.4 数据流总览

```
前端 (TS) ──HTTP/JSON──▶ routers/* ──依赖注入(deps.py)──▶ 业务模块
     ▲                      │                              │
     │ WS(队列广播)          │ SSE(下载流)                   │
     └── server.py ◀────────┴──────────────────────────────┴──▶ SQLite(~/.rwmod.db)
          │                                                     │
          └── SteamCMD 子进程 / Steam Web API / Skymods ◀───────┘（下载链路）
```

- 队列状态变化 → `queue.on_update(broadcast_queue_update)` → `/ws` 广播（`server.py:87,324-326`）。
- 下载进度 → SSE 事件流（`routers/download.py:142-230`），阻塞型工作经 `asyncio.to_thread` 移出事件循环（`routers/download.py:45`、`routers/mods.py:248`）。
- 自动更新发现新版本 → WS `update_notification`（`server.py:90,329-340`）。

---

## 4. 核心功能与实现要点

### 4.1 下载（SteamCMD 主源 + Skymods 备用源）

- **入口**：`downloader.download_one()`（`src/rwmod/downloader.py:124-141`）：per-mod 锁串行化 → `SteamCMD.workshop_download` 重试（最多 3 次，间隔 5s，`downloader.py:22-23,219-245`）→ 拷贝到 Mods 目录 → 锁外下载合集子项/依赖（防死锁，`downloader.py:135-141`）。
- **workshop_id 数字校验（最后防线）**：`steamcmd.py:159-165` 非数字直接拒绝，堵死 SteamCMD 命令注入（`+force_install_dir` 等 token）。
- **错误分类**：解析 `workshop_log.txt` 本次运行追加段 → OK/FILE_NOT_FOUND/ACCESS_DENIED/NO_MATCH/TIMEOUT 等；`NON_RETRYABLE` 跳过重试与 Skymods（`steamcmd.py:78-104,248-305`）。
- **超时**：`proc.communicate(timeout=10min)` 真正终止卡死进程（`steamcmd.py:122,200-215`）。
- **取消下载**：`cancel_download()` 注册表 `_live_procs` 记录存活进程，Windows 下发 CTRL_BREAK 覆盖子进程组（`steamcmd.py:30-64`）。
- **force 覆盖原子化**：旧版 rename 暂存 `.rwmod_stash_*` → 下载成功再删旧，失败自动回滚（`downloader.py:158-185,328-348`）。
- **Skymods 回退**：搜索页解析下载 URL → 白名单域名+私网 IP 拒绝（SSRF）→ 大小上限 → gzip/zip 校验 → `safe_extract_zip` → 拷贝（`skymods.py:40-61,73-97,129-226`）；重定向递归上限 5 层（`skymods.py:126`）。

### 4.2 合集下载

- 三层拉取：GetCollectionDetails API（专用端点，POST，修复了 405）→ 精确 HTML 爬取（`id="sharedfile_<id>"` 模式，无侧栏噪音）→ 用户 API Key（私集合）（`workshop.py:136-194,208-258`）；API+爬取双源**并集**去重，593 子项验证不丢（`workshop.py:158-170`）。
- SSE 集合流：一次性构建 installed 映射（避免 500+ 子项逐项全目录扫描）（`routers/download.py:188`）。

### 4.3 队列（并发控制 + 持久化 + 实时推送）

- 单例 `DownloadQueue`，`MAX_CONCURRENT=3` 信号量（`queue.py:23,39`）；单一常驻 worker + `asyncio.Event` 唤醒（修复运行中新增项被丢弃，`queue.py:157-171,194-229`）。
- 状态持久化到 `download_queue` 表，重启恢复 pending/downloading（`queue.py:330-347,359`）。
- 取消语义：下载中置 cancelled 并杀进程，后续任务不再改回（`queue.py:87-124,271-281`）。
- worker 异常兜底：单次 OSError 不杀 worker（`queue.py:219-226`）。

### 4.4 更新 / 备份 / 撤销

- 更新检测：本地 `last_updated`（0.5.0 起入库，`database.py:47-56,324-348`）对比 Steam `time_updated`，批量并发拉取（`workshop.py:342-361,364-447`）；`.rwmod_last_updated` 旧标记文件自动迁移清理（`workshop.py:417-426`）。
- 自动更新：`AutoUpdateManager` 24h 后台轮询 + 手动 `run_check`（发现更新即入队并 `force=True` 覆盖，`autoupdate.py:20,51-93`）；仅显式触发，打开首页不会自动检查（`routers/dashboard.py:63-68`）。
- 备份：覆盖前 zip 旧版到 `{backup_dir}/{wid}__{folder}__{ts}.zip`（`backup.py:25-67`）；恢复=先解压临时目录再原子 rename 交换+失败回滚（`backup.py:112-163`）；每个 mod 保留 5 份（`backup.py:213-232`）。
- 撤销：排序/恢复 profile 前快照 ModsConfig.xml 到 `~/.rwmod/undo/`，最多 20 份（`undo.py:20-39`；调用点 `routers/rimsort.py:140-142`、`routers/profiles.py:56-61`）。

### 4.5 排序 / 兼容 / 健康

- 拓扑排序（Kahn 算法，deque 优化）：loadTop/loadBottom/loadAfter/loadBefore → sorted/unresolved/cycles（`rimsort.py:226-306`）；应用排序=备份→快照→临时文件原子替换（`routers/rimsort.py:132-156`）。
- 排序分析：Harmony 首位、Core/DLC 顺序、已知冲突（RimPy 社区库优先 + 内置 4 条硬编码回退）、重复项（`load_order.py:26-45,98-228`）。
- 依赖/互斥：扫描 About.xml 的 `modDependencies`/`incompatibleWith`（`compat.py:25-131`）。
- 版本兼容：`Version.txt`/Core About.xml 检测 RimWorld 版本 vs `supportedVersions`（`compatibility.py:19-91`）。
- 健康检查：按 `time_updated` 距今 90 天/365 天分 maintained/stale/abandoned/removed（`routers/mods.py:123-163`）。
- 存档分析：正则提取 `<modIds>`，50MB+ 存档只读前 2MB（`save_parser.py:16-18,42-63`）；按存档缺失 Mod 补下载（`routers/saves.py:73-113`）。

### 4.6 迁移 / 标签 / 汉化检测

- `.rwmod` 包：manifest.json + profiles/ + backups/ + tags.json + config.toml；**密钥默认脱敏**，`include_secrets` 显式开启（`transfer.py:26-30,33-102,200-210`）；导入走 `safe_extract_zip` + 恢复非路径配置（`transfer.py:105-162,213-225`）。
- 标签：`mod_tags` 表（folder, tag）复合主键（`tags.py`）。
- 汉化检测：`Languages/ChineseSimplified` 等目录探测（`routers/mods.py:378-422`）。

### 4.7 CLI（13 命令，`cli.py`）

`setup`(L27) / `web`(L45，打包 EXE 的图形入口) / `config`(L62) / `download`(L75) / `import`(L118) / `import-collection`(L142) / `import-sort`(L177) / `list`(L219) / `profile-save|list|restore|delete`(L265-322) / `backup-list|restore|cleanup`(L328-368) / `compat`(L374) / `check-order`(L404)。

### 4.8 一次典型下载的完整调用链（单 Mod，非合集）

1. 前端「下载」面板 → `POST /api/download {ids:[...]}`（`routers/download.py:120-139`），`extract_mod_id` 过滤出纯数字 ID（`utils.py:40-58`）。
2. `_bounded_download` 进入全局信号量（并发 ≤2，`routers/download.py:37-48`）→ `asyncio.to_thread(download_one, ...)` 移到线程池。
3. `download_one` 取 per-mod 锁（`downloader.py:37-53,136`）→ `_find_existing` 查 `local_mod_metadata` 快路径或回退目录扫描（`downloader.py:56-101`）。
4. 已安装且非 force → 直接返回；force → 旧目录 rename 暂存 `.rwmod_stash_*`（`downloader.py:161-185`）。
5. `SteamCMD.workshop_download`：数字校验 → Popen（list 参数无 shell）→ 10 分钟超时 → 解析 workshop_log 增量段分类错误（`steamcmd.py:154-246`）。
6. 成功 → 从 `steamapps/workshop/content/294100/<id>` 读 About.xml 定目录名 → `copytree` 到 Mods 目录 → 写 `last_updated` 时间戳（`downloader.py:248-291`）。
7. 失败且可重试 → 最多 3 次；仍失败 → `try_skymods`（SSRF 校验 + 安全解压，`skymods.py:73-97`）；force 场景自动回滚暂存/备份（`downloader.py:293-325`）。
8. 下载完成 → 锁外 BFS 收集未装依赖并逐个下载（`downloader.py:389-421`）。
9. 结果写 `download_history`（`database.py:214-229`），队列状态经 WS 广播（`queue.py:137-141`）。

### 4.9 CLI 命令一览（`cli.py`，入口 `rwmod`）

| 命令 | 行号 | 说明 |
|---|---|---|
| `setup` | 27 | 交互式配置 SteamCMD/Mods/游戏目录 |
| `web` | 45 | 启动 Web UI（打包 EXE 的默认入口，`rwmod.iss:23` 快捷方式带 `web` 参数） |
| `config` | 62 | 显示配置 |
| `download <id...>` | 75 | 下载（支持 URL，`--force`/`--quiet`） |
| `import <file>` | 118 | 批量导入 modlist |
| `import-collection <id>` | 142 | 下载合集（SteamCMD 元数据 + 目录解析） |
| `import-sort <xml>` | 177 | 按 ModsConfig.xml 补缺失 |
| `list` | 219 | 列出已安装 Mod |
| `profile-save/list/restore/delete` | 265-322 | 配置档案管理 |
| `backup-list/restore/cleanup` | 328-368 | 备份管理 |
| `compat` | 374 | 版本兼容检查 |
| `check-order` | 404 | 排序分析 |

---

## 5. API 概览

OpenAPI 文档位于 `/api/docs`、`/api/openapi.json`（`server.py:107-108`）。除 `/api/auth/login`、`/api/status`、`/api/locale`(GET) 外均需 JWT（本机免密钥策略下本机请求自动放行，见 §7）。

| 方法 | 路径 | 功能 | 文件:行 |
|---|---|---|---|
| POST | /api/auth/login | 登录换 token（限速+恒定时间比较） | routers/auth.py:38 |
| GET | /api/auth/verify | 校验 token | routers/auth.py:63 |
| GET | /api/config | 读配置（密钥脱敏为 has_steam_api_key） | routers/config.py:78 |
| POST | /api/config | 写配置（路径黑名单校验） | routers/config.py:95 |
| GET | /api/dashboard | 统计+健康汇总 | routers/dashboard.py:57 |
| GET | /api/steamcmd/check | SteamCMD 就绪检测（非网络） | routers/dashboard.py:100 |
| GET | /api/mods | Mod 列表（30s 缓存） | routers/mods.py:101 |
| GET | /api/mods/check-updates | 更新检测 | routers/mods.py:109 |
| GET | /api/mods/health | 健康检查（60s 缓存） | routers/mods.py:123 |
| GET | /api/mods/export | 导出 Mod JSON | routers/mods.py:166 |
| GET | /api/mods/export-collection | 反向生成 Workshop ID 列表 | routers/mods.py:190 |
| GET | /api/mods/compatibility | 版本兼容 | routers/mods.py:208 |
| POST | /api/mods/import-local | 上传 zip 导入本地 Mod（2GB 上限） | routers/mods.py:223 |
| POST | /api/mods/batch/delete | 批量删除（先备份） | routers/mods.py:312 |
| POST | /api/mods/batch/download | 批量下载（共享信号量） | routers/mods.py:351 |
| GET | /api/mods/localization | 汉化检测 | routers/mods.py:398 |
| POST | /api/download | 下载若干 Mod | routers/download.py:120 |
| GET | /api/download/stream | SSE 下载流（合集/单 mod，30s 心跳） | routers/download.py:142 |
| POST | /api/import/file | 上传 modlist.txt 批量下载（5MB 上限） | routers/download.py:233 |
| POST | /api/import/collection | 合集 ID 下载 | routers/download.py:257 |
| POST | /api/import/sort | 上传 ModsConfig.xml 补缺失 | routers/download.py:280 |
| GET | /api/search | Workshop 搜索 | routers/workshop.py:20 |
| GET | /api/workshop/{mod_id} | Mod 详情 | routers/workshop.py:46 |
| GET | /api/collection/preview/{id} | 合集预览（已装/新增/曾失败） | routers/workshop.py:57 |
| POST | /api/mods/dependencies | 依赖树查询 | routers/workshop.py:105 |
| GET | /api/queue | 队列快照 | routers/queue.py:14 |
| POST | /api/queue/add / start | 入队 / 启动 | routers/queue.py:22,34 |
| DELETE | /api/queue/{mod_id} | 移除/取消（杀进程） | routers/queue.py:48 |
| POST | /api/queue/clear | 清已完成 | routers/queue.py:57 |
| GET | /api/backups | 备份列表 | routers/backups.py:12 |
| POST | /api/backups/{wid}/restore | 恢复备份 | routers/backups.py:26 |
| DELETE | /api/backups/{filename} | 删除备份（拒绝穿越） | routers/backups.py:45 |
| POST | /api/backups/cleanup | 清理旧备份 | routers/backups.py:56 |
| GET | /api/compat/check | 依赖缺失+冲突 | routers/compat.py:13 |
| GET/POST/DELETE | /api/profiles... | 档案 CRUD | routers/profiles.py:12-71 |
| POST | /api/rimsort/generate / compare / sort / apply | RimSort 系列 | routers/rimsort.py:21-163 |
| GET | /api/rimsort/check-order | 排序分析 | routers/rimsort.py:76 |
| GET | /api/history(+/stats,clear) | 下载历史 | routers/history.py:11-28 |
| GET/POST | /api/auto-check/result, /auto-update/run 等 | 自动更新 | routers/auto_update.py:12-42 |
| GET | /api/metrics | Prometheus 指标 | routers/metrics.py:62 |
| GET/POST/DELETE | /api/tags... | 标签 | routers/tags.py:20-70 |
| GET | /api/saves (+/{name}, /analyze, /download-missing) | 存档 | routers/saves.py:14-113 |
| POST | /api/transfer/export / import | .rwmod 迁移 | routers/transfer.py:43,80 |
| GET/POST | /api/undo | 撤销快照/回滚 | routers/undo.py:13-35 |
| GET | /api/status / /api/locale / /api/onboarding/check | 状态/i18n/引导 | routers/health.py:10-73 |
| WS | /ws | 队列实时广播（子协议鉴权） | server.py:241-284 |

### 5.1 鉴权矩阵

| 客户端场景 | 是否需要 token | 依据 |
|---|---|---|
| 本机 localhost，未设 RWMOD_SECRET | 否（免密钥直通） | auth.py:139-155 |
| 本机 localhost，设置了强 RWMOD_SECRET（严格模式） | 是 | auth.py:63-70,154-155 |
| 局域网客户端（任意配置） | 是（JWT） | auth.py:154-162 |
| WebSocket `/ws` | 是（子协议 `rwmod.<token>` 或旧 `?token=`） | server.py:226-260 |
| 公开端点（login / status / GET locale） | 否 | routers/auth.py:38、routers/health.py:10-23 |

错误响应统一为 `{"error": "<异常类名>", "detail": "<中文信息>"}`，由全局 `RwmodError` 处理器映射（`server.py:172-179`）；未捕获异常兜底 500（`server.py:182-189`）。每个响应带 `X-Request-ID` 与耗时日志（`server.py:143-166`）。

---

## 6. 前端

- **技术栈**：Vanilla TypeScript（无框架、无运行时依赖），Vite 8 构建（产物含 rolldown-runtime chunk），tsc --noEmit 类型检查（`frontend/package.json:10-14`、`frontend/vite.config.ts:1-33`）。
- **结构**（frontend/src/）：`main.ts`(625 行，布局+面板懒加载注册表+全局 fetch 鉴权补丁) / `api.ts`(统一 fetchJSON + SSE 下载流) / `ws.ts`(WebSocket 客户端+断线 REST 轮询兜底) / `auth.ts`(token 管理+登录弹窗) / `router.ts`(hash 路由) / `cmd.ts`(Ctrl+K 命令面板) / `toast.ts` / `style.css` / `panels/*`（14 个面板：dashboard/download/collection/import/mods/search/queue/rimsort/profiles/history/backup/saves/tags/config/updates）。
- **面板懒加载**：仅在首次进入时 `import()` 对应模块，按 `manualChunks` 拆包（`main.ts:25-104`、`vite.config.ts:9-18`；产物见 static/assets/panel-*.js）。
- **鉴权集成**：启动先 `ensureAuth()`（verify→登录弹窗）；`patchFetchWithAuth()` 给所有 `/api` fetch 注入 `Authorization: Bearer`，401 统一清 token 弹登录（`main.ts:596-625`）；token 存 localStorage `rwmod_token`（`auth.ts:15-27`）。
- **WS 通道**：token 走 `Sec-WebSocket-Protocol: rwmod.<token>` 子协议而非 URL 参数（`ws.ts:46-54`）；重连 5 次失败后每 5s REST 轮询 `/api/queue` 兜底，恢复即切回（`ws.ts:75-116`）。
- **XSS 防护**：模板字符串统一 `esc()` 转义（`main.ts:549-553`、`queue.ts:102-105` 等）。
- **i18n 现状**：后端 `i18n.py` 内置 zh-CN/en 词典（`i18n.py:77-158`），但仅覆盖导航/通用文案；各面板 HTML 仍为硬编码中文（`main.ts:107-481`），`/api/locale` 切换接口前端未真正消费 —— 属半成品。
- **构建**：`justfile` 的 `start`/`build` 将前端构建输出到 `static/`（`justfile:8,11-15`）；Docker 第一阶段 npm install + vite build（`Dockerfile:7-12`）。

### 6.1 前端面板 ↔ 后端 API 映射

| 面板（main.ts 布局 id） | 懒加载模块 | 主要 API |
|---|---|---|
| dashboard 首页 | panels/dashboard.ts | GET /api/dashboard、POST /api/auto-update/run、GET /api/queue |
| download 下载 | panels/download.ts | POST /api/download、GET /api/download/stream(SSE)、POST /api/mods/dependencies |
| collection 合集 | panels/collection.ts | GET /api/collection/preview/{id}、POST /api/import/collection |
| import 导入 | panels/import.ts | POST /api/import/file、POST /api/import/sort |
| mods 已装列表 | panels/mods.ts | GET /api/mods、/api/mods/health、/api/mods/check-updates、/api/mods/export-collection |
| search 搜索 | panels/search.ts | GET /api/search |
| queue 队列 | panels/queue.ts + panels/updates.ts | GET /api/queue、POST /api/queue/start、DELETE /api/queue/{id}、/api/auto-update/* |
| rimsort 排序 | panels/rimsort.ts | POST /api/rimsort/generate|compare|sort|apply、GET /api/rimsort/check-order |
| profiles 配置档案 | panels/profiles.ts | GET/POST/DELETE /api/profiles*、GET/POST /api/undo |
| history 历史 | panels/history.ts | GET /api/history(+/stats)、POST /api/history/clear、/api/auto-update/run |
| backups 备份 | panels/backup.ts | GET /api/backups、POST /api/backups/{id}/restore、DELETE /api/backups/{f}、POST /api/backups/cleanup |
| saves 存档 | panels/saves.ts | GET /api/saves、POST /api/saves/analyze |
| tags 标签 | panels/tags.ts | GET/POST/DELETE /api/tags* |
| config 配置 | panels/config.ts | GET/POST /api/config、GET /api/steamcmd/check、POST /api/transfer/export|import |



---

## 7. 安全模型

威胁模型（SECURITY.md:22-35）：**恶意局域网对端 + 构造的上传文件**（mod zip / ModsConfig.xml / .rwmod / modlist）。以下防护均为代码内实现：

### 7.1 鉴权（0.4.5 重写，0.5.0 收紧）

- 密钥来源：`RWMOD_SECRET` 环境变量，或首次启动随机生成并持久化到 `~/.rwmod.secret`（`auth.py:52,79-118`）；硬编码默认值 `rwmod-dev-secret`/`secret`/`password` 等被拒绝（`auth.py:49`）。
- 分层策略：**本机（127.0.0.1/::1/localhost）免密钥直通**，仅局域网客户端需 JWT；设置强 `RWMOD_SECRET` 后本机也需登录（`auth.py:63-76,139-163`）。设计理由见 `auth.py:144-153`（本机进程本就能读写 Mods 目录）。
- 登录防护：`hmac.compare_digest` 恒定时间比较 + 每 IP 10 次失败/5 分钟封禁（`routers/auth.py:18-35,54`）。
- token：HS256 JWT，TTL 7 天（`auth.py:44-45,121-136`）。
- WS 鉴权：`Sec-WebSocket-Protocol: rwmod.<token>`（旧 `?token=` 兼容），避免 token 进访问日志/浏览器历史（`server.py:223-260`）；`/api/metrics`、`/api/onboarding/check` 同样要求认证（`routers/metrics.py:63`、`routers/health.py:36`）。
- 响应头加固：nosniff/DENY 框架/no-referrer，API 响应 no-store（`server.py:122-140`）；CORS 仅放行 localhost 来源（`server.py:113-119`）。

### 7.2 workshop_id 数字校验链（防注入/穿越）

- `extract_mod_id` 只接受 `^\d+$` 或 URL `?id=<数字>`（`utils.py:40-58`）。
- SteamCMD 参数最后防线 `mod_id.isdigit()`（`steamcmd.py:159-165`）。
- 备份文件名数字校验（`backup.py:45-47`）；恢复路径数字校验（`backup.py:89-90`）。
- `PublishedFileId.txt` 非数字一律置空（`metadata.py:65-69`；`parser.py:173-176` 同理）。

### 7.3 zip 安全（穿越 + 炸弹）

- `safe_extract_zip`：拒绝绝对路径/`..`/越界成员、重复路径；成员数 ≤50000、解压总量 ≤4GB，且**解压前**全部校验（`utils.py:84-127`）。备份/恢复/导入/迁移全链路复用（`backup.py:119`、`skymods.py:200`、`routers/mods.py:265`、`transfer.py:117`）。
- 备份接口文件名 `_resolve_backup_path` 只接受纯文件名（`backup.py:238-254`）；批量删除同样拒绝 `..`/分隔符（`routers/mods.py:333-336`）。

### 7.4 SSRF 防护（Skymods 回退）

- 下载 URL 白名单域名（smods.ru / modsbase.com）+ 拒绝私网/回环/链路本地/保留 IP + 响应大小上限（页面 2MB / 下载 1GB）（`skymods.py:35-61`）。
- Steam API 调用全部硬编码 HTTPS 官方域名（`workshop.py:47,95,240,279`，带 `nosec B310` 注释）。

### 7.5 上传限制（0.5.0 强制生效）

- `read_upload_limited`：读 `max_bytes+1` 字节做硬上限，chunked 上传（无 Content-Length）不再绕过大小的检查（`utils.py:130-150`）；`routers/transfer.py` 的 `_save_upload` 流式写盘+计数截断（`routers/transfer.py:24-40`）。
- 各端点上限：modlist 5MB（`routers/download.py:32`）、mod zip 2GB（`routers/mods.py:23`）、.rwmod 2GB（`routers/transfer.py:20`）。

### 7.6 配置路径黑名单（0.5.0 新增）

- 拒绝相对路径、磁盘根、程序运行目录、Windows 系统目录（`C:\Windows`、`Program Files`、`ProgramData`、`$Recycle.Bin`、`C:\Users` 本身）（`routers/config.py:19-60`）。

### 7.7 密钥脱敏

- `/api/config` 不回传 `steam_api_key`，只回 `has_steam_api_key`（`routers/config.py:83-92`，测试 `tests/test_api.py:233-238`）。
- `.rwmod` 导出默认不含密钥，manifest 记录 `has_steam_api_key`（`transfer.py:50-59,200-210`，测试 `tests/test_security_batch.py:126-168`）。

### 7.9 安全加固时间线（按版本，来源 CHANGELOG.md + 提交）

| 版本 | 加固内容 | 主要落地位置 |
|---|---|---|
| 0.4.1 | 备份/恢复路径穿越修复、SteamCMD 10 分钟超时真正终止、Skymods 解压防护 | backup.py:238-254、steamcmd.py:200-215、skymods.py:199-203 |
| 0.4.2 | 合集接口改用专用端点（顺带修复 405/命令路径） | workshop.py:85-120 |
| 0.4.3 | 强制启用认证（修复默认全放行）、SSE 心跳、WS 广播解阻塞 | auth.py:139-163、server.py:287-321 |
| 0.4.4 | 前端登录/401 拦截落地 | frontend/src/auth.ts、main.ts:596-625 |
| 0.4.5 | **鉴权模型重写**（本机免密钥）、workshop_id 校验链、Skymods SSRF、zip 炸弹防护、上传上限统一、config 密钥脱敏、/ws 与 /metrics 收紧 | auth.py:49-163、steamcmd.py:159-165、skymods.py:35-61、utils.py:84-150、routers/config.py:83-92 |
| 0.5.0 | 上传按字节硬上限（修复 chunked 绕过）、配置路径黑名单、.rwmod 导出密钥脱敏、WS token 移出 URL、force 覆盖原子化、取消下载真正杀进程 | utils.py:130-150、routers/config.py:19-60、transfer.py:200-210、server.py:223-260、downloader.py:158-185、steamcmd.py:34-64 |

### 7.8 其他

- 子进程一律 list 参数、无 shell（`steamcmd.py:166-187`，`nosec B603` 注释说明）。
- SQL 拼接列名白名单（`database.py:354-379`，`nosec B608` 注释说明）。
- 上传端点兜底：`routers/saves.py:53-65` 与 `routers/rimsort.py:52-73` 仅做了 `_reject_oversized`（依赖 Content-Length）后仍 `await file.read()` —— **chunked 上传下大小检查仍可被跳过**（见 §13 风险）。

---

## 8. 数据存储

全部持久化数据在用户主目录，无需数据库服务：

| 位置 | 内容 | 依据 |
|---|---|---|
| `~/.rwmod.db` | SQLite：download_history / mod_cache / local_mod_metadata / mod_tags / download_queue | database.py:38,146-195 |
| `~/.rwmod.toml` | 配置（steamcmd/mods/rimworld/backup 路径 + steam_api_key） | config.py:18 |
| `~/.rwmod.secret` | 自动生成的访问密钥 | auth.py:52 |
| `~/.rwmod.log` | 滚动日志 5MB×3 | logger.py:12-14 |
| `~/.rwmod/profiles/` | ModsConfig.xml 快照 | profile.py:26 |
| `~/.rwmod/undo/` | 撤销快照（最多 20） | undo.py:20-21 |
| `~/.rwmod/rimpy_database.json` | RimPy 社区库缓存（24h） | rimpy_db.py:36-37,27 |
| `<mods_dir>/_backups/` | 备份 zip | config.py:56 |
| `steamcmd/steamapps/...` | SteamCMD 下载缓存 | steamcmd.py:133-136 |

- **并发模型**：每线程一个 SQLite 连接（WAL + busy_timeout=5000 + 8MB cache），`close_db` 按 id() 追踪已关闭连接（`database.py:60-102,105-120`）；init_db 锁非重入防死锁（`database.py:123-134`）。
- **迁移框架**：`PRAGMA user_version` + 有序迁移列表，事务内执行、失败回滚（`database.py:41-56,137-211`）；v1 迁移 = `local_mod_metadata.last_updated` 列（替代旧 `.rwmod_last_updated` 标记文件）。
- **元数据缓存**：内存 TTL 30s（按目录 mtime 失效）→ SQLite 增量（按 folder mtime 跳过未变文件夹）→ 全量扫描兜底（`mod_cache.py:26-95`、`cache_db.py:18-102`）。

### 8.1 SQLite 表一览（`~/.rwmod.db`，DDL 见 `database.py:144-197`）

| 表 | 用途 | 关键列 |
|---|---|---|
| download_history | 下载历史（状态机 pending/success/failed/skipped） | workshop_id, status, msg, created_at |
| mod_cache | Steam 远程元数据缓存（标题/作者/更新时间） | workshop_id(PK), time_updated, cached_at |
| local_mod_metadata | 本地 Mod 元数据 + workshop_id 索引 + last_updated | folder(PK), package_id, workshop_id, dir_mtime, last_updated |
| mod_tags | 标签（folder, tag 复合主键） | folder, tag |
| download_queue | 队列持久化（重启恢复） | workshop_id(UNIQUE), status, progress, msg, updated_at |

索引：download_history(workshop_id, created_at)、local_mod_metadata(workshop_id)、download_queue(status)（`database.py:174-176,195`）。

### 8.2 实时通道消息格式

- **WebSocket /ws**（`server.py:263-279`）：客户端 `{"cmd":"ping"|"subscribe"}` → 服务端 `{"type":"pong","queue_pending":N}` / `{"type":"subscribed",...}` / `{"type":"echo",...}`；服务端主动推送 `{"type":"queue_update","items":[...]}`（`server.py:324-326`）与 `{"type":"update_notification","count":N,"updates":[...]}`（`server.py:329-340`）。
- **SSE /api/download/stream**（`routers/download.py:170-230,315-318`）：事件行 `data: {"event":"start|info|skip|ok|warn|fail|done", ...}`，下载间隙每 30s 发心跳注释行防反代掐断（`routers/download.py:51-69`）。

---

## 9. 质量与测试

### 9.1 测试（254 项，19 个文件，`pytest --collect-only` 实测）

| 文件 | 测试数 | 覆盖 |
|---|---|---|
| test_api.py | 36 | 端点集成（TestClient + token fixture） |
| test_steamcmd.py | 26 | 错误分类/超时/取消进程 |
| test_workshop.py | 20 | 搜索/合集/依赖/更新检测 |
| test_downloader.py | 19 | ID 解析/查找/命名/force 回滚 |
| test_security_batch.py | 15 | 上传上限/路径校验/密钥脱敏/WS 子协议 |
| test_skymods.py | 15 | SSRF/重定向/zip 防护 |
| test_errors.py | 15 | 异常体系 |
| test_parser.py | 13 | modlist/合集 bin/JSON/ModsConfig |
| test_config.py | 12 | 配置读写/缓存 |
| test_backup.py | 11 | 备份/恢复/穿越防护 |
| test_tags.py / test_save_parser.py / test_compatibility.py | 11/11/10 | 标签/存档/兼容 |
| test_queue_persistence.py / test_database.py | 8/10 | 队列持久化/DB 迁移 |
| test_rimsort.py / test_load_order.py / test_queue_cancel.py / test_metadata.py | 7/5/5/5 | 排序/分析/取消/元数据 |
| **合计** | **254** | 与 CHANGELOG.md:44「254 通过」一致 |

- 网络标记：`pytest -m "not network"` 为离线门禁（`pyproject.toml:68-71`）；CI 同款（`.github/workflows/ci.yml:49`）。
- **本机复跑说明**：在本次调查的受限 shell（沙箱禁止写工作区外临时目录）下复跑，73 通过 / 181 因 `PermissionError`（无法访问 `%TEMP%\dsh-*\pytest-of-*`）报错，属**环境限制而非项目缺陷**；项目声明的 254 项通过由 Linux CI 与开发机验证。
- conftest：隔离的 Config/DB 路径 + 重置队列/自动更新单例 + 注入 token（`tests/conftest.py:14-58`）。

### 9.2 静态质量

- ruff（E/F/W/I/UP/B/SIM，line-length 100）+ 按目录豁免（`pyproject.toml:46-58`）。
- mypy strict（`pyproject.toml:74-87`），routers 按 FastAPI 惯例豁免 no-untyped-def。
- bandit `-ll` 硬性门禁（`pyproject.toml:107-114`、`.github/workflows/ci.yml:67`）；跳过 B101/B314/B405 并附理由（assert 测试、本地 XML 可接受）。
- pre-commit：ruff / codespell / bandit / mypy / 基础钩子（`.pre-commit-config.yaml`）。
- CI 7 个 job：lint / typecheck / test(+Codecov) / security(bandit+pip-audit advisory) / quality(radon 圈复杂度) / frontend(tsc) / docker（`.github/workflows/ci.yml:11-110`）。
- 测试统计口径：README 声称 0.4.5 时覆盖率 49%→56%（CHANGELOG.md:194）。

### 9.3 一键检查脚本 `run_tests.py`

6 步串联：①新模块导入冒烟（8 项断言，含版本断言）→ ②ruff lint → ③ruff format --check → ④pytest（离线）+ 覆盖率 → ⑤mypy strict → ⑥bandit -ll（`run_tests.py:56-207`）。缺失工具时自动 pip 安装（`run_tests.py:40-53`）。**注意**：第 ① 步的版本断言仍写死 `0.4.5`（`run_tests.py:88`），与当前 0.5.0 不符（见 §12 风险 1）。

---

## 10. 构建与分发

### 10.1 源码运行

`uv sync` + 前端构建（bun 或 npm 皆可）→ `uv run uvicorn rwmod.server:app` 或 `start.bat`（README.md:75-88、`start.bat`）。

### 10.2 PyInstaller（`rwmod.spec`）

- 入口 `cli.py`，`static/` 作为数据打包，`hiddenimports` 全量列出 Web 依赖（uvicorn 字符串导入无法静态追踪，`rwmod.spec:7-79`）。
- **硬性排除** torch/cv2/pandas/numpy 等无关重库，防止误打包（`rwmod.spec:83-122`）。
- steamcmd 以 Tree 打包**仅程序本体**，排除 steamapps/userdata/depotcache/appcache/logs/siteserverui 等缓存（`rwmod.spec:131-145`）。
- 实测产物：`dist/rwmod.exe` 81.8MB、`dist/rwmod_setup.exe` 120MB；`pyinstaller_err.txt` 实为成功构建的 INFO 日志（"Build complete"）。

### 10.3 Inno Setup（`rwmod.iss`）

- AppVersion 0.5.0，安装到 `{commonpf}\rwmod`，**需要管理员权限**（`rwmod.iss:3,6,13`）；桌面+开始菜单快捷方式带 `web` 参数（`rwmod.iss:22-25`）；steamcmd 目录同步排除缓存（`rwmod.iss:20`）。

### 10.4 Docker（`Dockerfile` + `docker-compose.yml`）

- 三阶段：node 22 构建前端 → python 3.13 装依赖 → slim 运行时；非 root 用户 + Mods/steamcmd 挂载点 chown（`Dockerfile:7-51`）；compose 挂载 `./steamcmd`(ro)、mods/config 卷 + 健康检查（`docker-compose.yml:9-29`）。

### 10.5 目录说明

- `steamcmd/`：本地完整 SteamCMD 安装（**1.86GB**，含下载缓存；**未纳入 git**，`git ls-files` 计数 0）。
- `static/`：前端构建产物（`index.html` + assets 已构建 js/css）；`static/assets/` 被 gitignore（`.gitignore:18-20`），仅 `index.html` 入库。
- `dist/`、`build/`：打包产物（gitignore）。

---

## 11. 近期开发动态（git）

- **当前状态**：`main` 分支，领先 `origin/master` 27 个提交，工作树干净（git status）。
- 最近 30 条提交集中在 2026-07-17 ~ 2026-08-07，节奏约 1-2 天一个版本：
  - `7439949`（HEAD）chore: uv lock 补全缺失依赖
  - `8fe1323` fix: mypy 跨平台（CTRL_BREAK 动态访问 + defusedxml 存根）
  - `802e66a` **v0.5.0**：安全加固 + force 覆盖原子化 + SteamCMD 取消终止 + SQLite 迁移框架（+1380/-163，40 文件，含 5 个新测试文件）
  - `09b149e` **v0.4.5**：全面安全/并发/前端审查修复 + 新鉴权模型
  - `0020d5c` fix(frontend): 认证缺失导致 401/undefined
  - `2c3bccf` feat: 一键迁移 + 操作撤销
  - `dd9128e`/`d8449d5` **0.4.2**：合集下载修复（GetCollectionDetails 专用端点 + 双源合并）
  - `92dfe9c` **0.4.1**：全面安全与稳定性修复 + WebSocket 实时队列 + 打包体积优化
  - `76fe6cd` build: PyInstaller + Inno Setup 打包
  - `cea500c` **v0.4.0**：collection 3 层回退 + save parser + tags
  - `aecc407` 12-phase optimization（auth/queue/CI/observability/frontend/security/API-spec）
  - `1f8f7a7` v0.3.0（起点）
- 版本演进脉络：0.1.0（2025 初始）→ 0.3.0 → 0.4.0/0.4.1/0.4.2/0.4.3/0.4.4/0.4.5 → 0.5.0；v0.4.x 主线是**安全与并发修复**，v0.5.0 面向**公开发布 + 局域网部署**。

### 11.1 v0.5.0 提交（802e66a）改动明细（git log --stat 实测）

- **安全**：上传限读强制生效（utils.read_upload_limited 接入 transfer/mods/download 三个 router，`tests/test_security_batch.py` 新增 192 行）、配置路径校验（routers/config.py +74 行）、.rwmod 导出脱敏（transfer.py +32 行）、WS token 子协议化（server.py +40 行、frontend/src/ws.ts +25 行）。
- **行为**：force 覆盖原子化（downloader.py +99 行）、取消下载终止 SteamCMD（steamcmd.py +64 行、queue.py +39 行）。
- **架构**：SQLite user_version 迁移框架（database.py +130 行）、last_updated 入库（cache_db.py / workshop.py / downloader.py）、`_get_conn`→`get_conn`、死代码清理（profile.py -7 行、tags.py）。
- **测试**：新增 test_security_batch.py（192 行）、test_downloader.py（+117）、test_steamcmd.py（+49）、test_database.py（+98）、test_workshop.py（+67）；测试总数 223→254。
- **发布**：新增 LICENSE / SECURITY.md / README 合规声明；版本 0.4.5→0.5.0（pyproject / server / __init__ / rwmod.iss 同步）；重新编译安装包（排除 steamcmd 缓存）。

---

## 12. 亮点与风险点

### 亮点

1. **安全投入扎实且有闭环**：0.4.1→0.5.0 三轮专项加固，每条修复都有对应测试（如 `tests/test_security_batch.py`），威胁模型在 SECURITY.md 显式声明；workshop_id 校验链、safe_extract_zip、SSRF 白名单都是「入口→出口」的纵深防御。
2. **并发/可靠性细节讲究**：SQLite WAL+每线程连接、队列单一持久 worker+Event 唤醒、SSE 30s 心跳、WS 背压断连、force 覆盖原子回滚、备份恢复 rename 交换 —— 大量「血泪教训」都写成注释（如 `queue.py:157-164`、`downloader.py:158-161`、`backup.py:112-113`）。
3. **体积控制**：PyInstaller 硬排除重库 + steamcmd Tree 排除缓存，安装包 1.4GB→114MB（RELEASE_NOTES.md:126）。
4. **工程质量配套完整**：254 测试 + strict mypy + bandit 门禁 + pre-commit + 7-job CI + 覆盖率报告。
5. **部署形态多**：源码 / EXE 安装包 / Docker 三选一，Windows 用户零依赖开箱即用（安装包内置 SteamCMD）。

### 风险点

1. **`run_tests.py:88` 版本断言过期**：仍断言 `__version__ == '0.4.5'`，而当前是 0.5.0 —— 运行该脚本第 1 步「版本检查」必然失败（实测 `src/rwmod/__init__.py:3` = 0.5.0）。
2. **2 个上传端点仍可被 chunked 上传绕过大小限制**：`routers/saves.py:53-65` 与 `routers/rimsort.py:52-73` 仅 `_reject_oversized`（依赖 `UploadFile.size`）后直接 `await file.read()`；0.5.0 声明的「所有上传端点按字节硬上限读取」（CHANGELOG.md:6-9）未覆盖这两处。攻击面有限（需已认证的局域网对端），但与项目自己的安全声明不一致。
3. **`app_state.py` 是半成品**：lifespan 创建了 `AppState`（`server.py:82-84`），但所有路由仍用 `deps.py` 模块级单例，AppState 未被消费 —— 抽象与实现脱节，易误导后续维护。
4. **前端 i18n 半成品**：`i18n.py` 双语词典 + `/api/locale` 接口存在，但面板全部硬编码中文，`/api/locale` 无人调用；前端也没有语言切换 UI。
5. **`steamcmd/` 本地目录 1.86GB**：虽未入库且打包时排除缓存，但 Docker compose 默认把整个 `./steamcmd`（含缓存）只读挂载进容器（`docker-compose.yml:13`），磁盘占用与首次构建时间偏大。
6. **默认路径写死 D: 盘**：`Config` 默认 `D:/RimWorld/Mods`、`D:/RimWorld`（`config.py:46-47`），源码运行时非 D 盘用户须先配置（README 已说明，但对新手不友好）。
7. **仓库卫生**：根目录残留 `commit_msg.txt`、`pyinstaller_err.txt`（实际是成功日志）、`iscc_out.txt` 等杂项；`verify_fixes.py` 源码为 GBK 编码导致 UTF-8 下乱码。
8. **依赖树较小但边界多**：`routers/saves.py` 上传分析 `await file.read()` 整读（叠加第 2 点）；`mods.py` 健康检查对数百 mod 逐项 `read_mod_metadata`（有缓存兜底，TTL 60s）。

---

## 附：调查环境说明

- 本次调查在受限 shell（沙箱禁止访问工作区外临时目录）下运行，`pytest` 本地复跑出现 PermissionError 属环境限制；除测试复跑外，其余结论均基于源码静态阅读与 git 历史，未对仓库做任何修改（`git status` 工作树干净）。
- 报告输出文件：本文件（`docs/project-investigation-report.md`），仓库原有 `docs/agents/` 两个文件未改动。
