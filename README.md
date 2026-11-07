# rwmod — RimWorld Mod Manager

[![CI](https://github.com/qxkt222/rwmod-rimworld-mod-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/qxkt222/rwmod-rimworld-mod-manager/actions)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://python.org)
[![React 19](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev)

全离线 RimWorld Mod 管理器 — 无需 Steam 客户端即可匿名下载、更新、备份与管理 RimWorld Mod。
支持合集批量下载、依赖管理、排序分析、实时队列与多实例。

后端 FastAPI + SQLite（Python 3.13），前端 React 19 + Vite + Zustand，单文件 CLI / Web UI 双入口。

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 📥 **下载** | SteamCMD 匿名下载 + Skymods 备用源（默认关闭），支持合集批量下载 |
| 🔍 **搜索** | Steam Workshop 搜索（Web API，无需 API Key） |
| 🔄 **一键更新** | 自动检测可用更新，后台下载；部分批次失败会明确告知「结果可能不完整」 |
| 🔌 **实时队列** | WebSocket 推送队列状态，断线时界面明确提示 |
| ⚡ **批量下载** | 每批多个 Mod 共用 SteamCMD 进程，SSE 实时日志与进度 |
| 🛡 **安全加固** | 路径穿越/zip 炸弹防护、steamcmd 可执行路径校验、上传硬上限、CSP |
| 🧩 **依赖管理** | 下载前预览依赖树，自动补装缺失依赖 |
| 💾 **备份回滚** | 更新前自动备份旧版，一键恢复；失败保留旧内容而非删除 |
| 📊 **健康检查** | Mod 活跃度（维持/停更/废弃/下架） |
| ✅ **兼容检测** | 对比 RimWorld 版本，标出不兼容 Mod |
| 📐 **排序分析** | 官方内容锚定、Community Rules + 用户规则、声明规则 vs 实际顺序诊断 |
| 🗂 **Steam DB（可选）** | CC0 Workshop 数据库，默认关闭，仅显式 refresh 下载 |
| 💿 **配置档案** | 保存/切换 ModsConfig.xml 快照 |
| 🗃 **多实例** | 多个 Mods 目录/配置并存；队列、标签、备份、设置按实例隔离 |
| 🏷 **标签** | 给 Mod 文件夹打标签，按标签筛选（含创建/重命名/删除） |
| 📤 **合集导出** | 从已安装 Mod 反向生成 Workshop ID 列表 |
| 📦 **一键迁移** | 导出/导入 `.rwmod` 包（profiles/备份/标签/配置），跨机器迁移 |
| ↩️ **操作撤销** | 破坏性操作前自动快照 ModsConfig.xml，一键回滚 |
| 🚦 **批量启停** | Mod 列表勾选后批量启用/停用，写入前自动备份并可撤销 |
| 🛠 **Mod 设置** | 查看/编辑/备份/恢复 `Config/Mod_*.xml`，写入前需确认游戏已关闭 |

## 🚀 快速开始

### 安装包（Windows）

从 [Releases](https://github.com/qxkt222/rwmod-rimworld-mod-manager/releases) 下载
`rwmod_setup.exe`（安装版）或 `rwmod.exe`（免安装，内置 SteamCMD）。

### 从源码运行

```bash
# 1. 后端依赖
uv sync

# 2. 构建前端（必需）—— static/ 是构建产物、不随仓库分发；
#    缺少它时首页返回 503 并提示「前端未构建」
cd frontend
npm ci
npm run build
cd ..

# 3. 启动
uv run uvicorn rwmod.server:app --host 127.0.0.1 --port 8000
# 或：./start.bat
```

打开 http://127.0.0.1:8000

### Docker

```bash
docker compose up -d
```

### 升级

从 0.7.x 直接运行新的 `rwmod_setup.exe` **覆盖安装**即可，不需要先卸载：

- 配置（`.rwmod.toml`）与数据库（`.rwmod.db`）在启动时自动迁移到当前 schema；
- `Mods`、备份、存档、`Config/` 都在游戏目录，不受安装影响；
- 安装前先退出 rwmod（含后台/托盘进程），否则 exe 被占用会导致安装失败。

## 🔧 CLI 命令

```bash
rwmod download 1234567890           # 下载单个 Mod
rwmod import modlist.txt            # 批量导入
rwmod import-collection 3721899704  # 下载合集
rwmod list                          # 列出已安装
rwmod profile-save 原版             # 保存配置快照
rwmod compat                        # 检查兼容性
rwmod check-order                   # 分析排序
```

## 📊 API 文档

启动服务后访问 http://127.0.0.1:8000/docs 查看交互式 OpenAPI 文档。
前端 TypeScript 类型由 `/api/openapi.json` 自动生成（`npm run gen:api`），不再手写接口定义。

## 🧪 开发与质量

```bash
# 后端一键门禁：import smoke / ruff / ruff format / pytest / mypy strict / bandit
python run_tests.py

# 单项
uv run pytest -m "not network"
uv run mypy src/rwmod/
uv run bandit -c pyproject.toml -r src/

# 前端
cd frontend
npm test          # Vitest 单元测试
npm run test:e2e  # Playwright（真实后端 + 临时数据目录）
npm run build
npm run gen:api   # 从 OpenAPI 重新生成类型
```

当前基线：

| 层 | 数量 |
|---|---|
| 后端 pytest | **1496 passed / 0 failed / 8 skipped**，覆盖率 91.7% |
| 前端 Vitest | **31 passed** |
| E2E Playwright | **82 passed / 0 failed / 0 skipped** |

E2E 会启动真实后端，但 HOME/USERPROFILE 指向临时目录，不会读写真实存档或配置。

## 🏗 架构

```
src/rwmod/
├── server.py            # FastAPI app + 中间件（CSP、body 上限、异常映射）
├── deps.py              # 共享单例注入（Config / DB / Queue）
├── errors.py            # 统一异常体系，失败以状态码承载
├── routers/             # 20 个路由模块（全部 /api 前缀）
├── download/            # 下载内核（locks / install / deps / outcomes / core）
├── steamcmd_support/    # SteamCMD 封装（logparse / procs / taxonomy）
├── models/              # Pydantic 响应模型
└── [业务模块]           # workshop / rimsort / profile / queue / database ...
```

```
frontend/src/
├── app/                 # 入口、bootstrap、命令与快捷键
├── router/              # 自写 hash router（含 #updates 归一化）
├── layout/              # AppShell / Sidebar / Topbar / PanelHost
├── store/               # Zustand 切片 + revision bus
├── api/                 # client / ws / sse / generated（OpenAPI 类型）
├── components/          # 命令面板、Toast、Dialog、ErrorBoundary 等
├── panels/              # 15 个面板
└── styles/              # tokens / base / layout
```

关键不变量：

- **失败契约**：失败抛 `errors.py` 异常，状态码承载结果；200 只放数据。
- **实例隔离**：所有写入按 `(instance_id, id)` 作用域，跨实例不串。
- **原子写**：ModsConfig / profile / undo 走 `mkstemp` + `os.replace`。
- **不可信 XML** 走 `xmlutil.parse_xml_root`。
- **前端 testid**：`panel-<name>` / `nav-<name>` / `action-<verb>` / `field-<name>` 等，
  详见 `frontend/tests/README.md`。

## 🔒 安全说明

- 本项目定位为**个人本机使用**，没有登录鉴权。API 对同一网络开放，
  **不要**不做反代/HTTPS 保护就暴露到公网。
- 上传：大小硬上限、zip 路径穿越/炸弹防护、workshop_id 数字校验。
- steamcmd 可执行路径只接受 `steamcmd.exe`，并禁止位于可上传目录内。
- HTML 文档响应带 `Content-Security-Policy`（`script-src 'self'`）。
- Skymods 默认关闭，首次失败熔断 10 分钟。
- 安全漏洞请通过 [SECURITY.md](SECURITY.md) 的流程私信提交。

## ⚖️ 合规声明

- **SteamCMD 匿名下载**：以 `anonymous` 身份下载创意工坊内容，请自行确认符合你所在
  地区与 Steam 订户协议的要求；仅供个人备份自用，勿用于商业分发或绕过付费内容。
- **内置 SteamCMD**：版权归 Valve 所有，分发遵循 Valve 条款。
- **Skymods (smods.ru)**：默认关闭；显式启用后仅作 SteamCMD 失败时的回退，
  抓取行为受第三方站点条款约束，内容版权归原作者。
- 本项目与 Ludeon Studios / Valve 无关联。RimWorld 为 Ludeon Studios 商标。

## 📝 License

MIT（详见 [LICENSE](LICENSE)）
