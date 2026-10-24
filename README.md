# rwmod — RimWorld Mod Manager

[![CI](https://github.com/qxkt222/rwmod-rimworld-mod-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/qxkt222/rwmod-rimworld-mod-manager/actions)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://python.org)

全离线 RimWorld Mod 管理器 — 无需 Steam 客户端即可匿名下载、更新、备份与管理 RimWorld Mod，支持合集批量下载、依赖管理、排序分析、实时队列与安全加固。

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 📥 **下载** | SteamCMD 匿名下载 + Skymods 备用源，支持合集批量下载 |
| 🔍 **搜索** | Steam Workshop 搜索（Web API，无需 API Key） |
| 🔄 **一键更新** | 自动检测可用更新，后台下载，队列管理 |
| 🔌 **实时队列** | WebSocket 实时推送队列状态，前端无需手动刷新 |
| ⚡ **批量下载** | 每批多个 Mod 共用一个 SteamCMD 进程，SSE/WS 实时进度条 |
| 🛡 **安全加固** | 备份/解压路径穿越防护、Steam API Key 脱敏、进程超时保护、Skymods 默认关闭 |
| 🧩 **依赖管理** | 下载前预览依赖树，自动补装缺失依赖 |
| 💾 **备份回滚** | 更新前自动备份旧版，一键恢复 |
| 📊 **健康检查** | Mod 活跃度（维持/停更/废弃/下架） |
| ✅ **兼容检测** | 对比 RimWorld 版本，标出不兼容 Mod |
| 📐 **排序分析** | 官方内容锚定、Community Rules + 用户规则、声明规则 vs 实际顺序诊断 |
| 🗂 **Steam DB（可选）** | CC0 Workshop 数据库（47MB / 57,679 条），默认关闭，仅显式 refresh 下载，不参与默认排序请求 |
| 💿 **配置档案** | 保存/切换 ModsConfig.xml 快照 |
| 🗃 **多实例** | 多个 Mods 目录/配置档案并存与切换，队列、标签、缓存、备份按实例隔离 |
| 📤 **合集导出** | 从已安装 Mod 反向生成 Workshop ID 列表 |
| 📦 **一键迁移** | 导出/导入 `.rwmod` 包（profiles/备份/标签/配置），跨机器迁移 |
| ↩️ **操作撤销** | 破坏性操作（排序/恢复）前自动快照 ModsConfig.xml，一键回滚 |
| 🚦 **批量启停** | Mod 列表勾选后批量启用/停用，写入前自动备份并可撤销 |
| 🛠 **Mod 设置** | 独立设置页查看/编辑/备份/恢复 `Config/Mod_*.xml`，写入前需确认游戏已关闭 |
| 🚀 **性能** | 大小快照 + 健康检查持久缓存：Mods 11ms / Dashboard 6–8ms / Health 11ms（553 Mod 实测） |

## 🚀 快速开始

```bash
# 安装 Python 依赖
uv sync

# 构建前端（必需一步）——static/ 是构建产物、不随仓库分发，
# 缺少它时首页会返回 503 并提示「前端未构建」
cd frontend && npm install && npm run build && cd ..
# 或使用 bun：cd frontend && bun install && bun run build && cd ..
#（bun.lock / package.json 二者皆可，Docker 构建使用 npm）
# 也可以用仓库脚本：python tools/build_frontend.py

# 启动服务
uv run uvicorn rwmod.server:app --host 0.0.0.0 --port 8000
# 或直接运行（会先自动构建前端）
./start.bat
```

打开 http://localhost:8000

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

启动服务后访问 http://localhost:8000/docs 查看交互式 OpenAPI 文档。

## 🧪 质量

```bash
# 测试
uv run pytest -m "not network"
uv run pytest --cov=rwmod --cov-report=html

# Lint
uv run ruff check src/
uv run ruff format --check src/

# 类型检查
uv run mypy src/rwmod/

# 安全扫描
uv run bandit -c pyproject.toml -r src/

# API 模糊测试
schemathesis run --base-url http://localhost:8000

# Pre-commit
pre-commit install
```

当前离线套件：**1382 passed / 0 xfailed**（12 deselected）；`run_tests.py` 六项门禁
**6/6 通过**。原有 33 个 xfail 已全部转正。

## 🏗 架构

```
src/rwmod/
├── server.py           # FastAPI app factory (415行)
├── deps.py             # 依赖注入 (Config/DB/Queue)
├── errors.py           # 统一异常体系
├── routers/            # 19 个路由模块（全部 /api 前缀）
│   ├── auto_update.py  # 自动更新
│   ├── backups.py      # 备份管理
│   ├── compat.py       # 兼容性检查
│   ├── config.py       # 配置管理
│   ├── dashboard.py    # 首页统计
│   ├── download.py     # 下载 / 导入 / SSE 流
│   ├── health.py       # 状态检测
│   ├── history.py      # 下载历史
│   ├── metrics.py      # Prometheus 指标
│   ├── mod_settings.py # Mod 设置查看 / 编辑 / 备份
│   ├── mods.py         # Mod 列表 / 健康 / 兼容 / 导出
│   ├── profiles.py     # 配置档案
│   ├── queue.py        # 下载队列
│   ├── rimsort.py      # RimSort 集成
│   ├── saves.py        # 存档分析
│   ├── tags.py         # Mod 标签
│   ├── transfer.py     # 一键导出 / 导入
│   ├── undo.py         # 操作撤销
│   └── workshop.py     # 搜索 / 依赖 / 合集预览
├── [业务模块]          # download/workshop/backup/etc.
├── models/             # Pydantic 响应模型
└── py.typed            # PEP 561 类型标记
```

## ⚖️ 合规声明

- **SteamCMD 匿名下载**：本工具通过 SteamCMD 以 `anonymous` 身份下载创意工坊内容。
  这利用了 SteamCMD 的公开能力，但**请自行确认**其符合你所在地区与 Steam 订户协议
  的要求；仅供个人备份与自用，请勿用于商业分发或绕过付费内容。
- **内置 SteamCMD**：安装包内置 SteamCMD 可执行文件。SteamCMD 版权归 Valve 所有，
  分发请遵循 Valve 的条款。
- **Skymods (smods.ru) 备用源**：默认关闭；显式启用后仅在 SteamCMD 下载失败时作为
  回退，抓取行为受第三方站点条款约束，内容版权归原作者。
- 本项目与 Ludeon Studios / Valve 无任何关联。RimWorld 为 Ludeon Studios 商标。

## 🔒 安全说明

- 本项目定位为**个人本机使用**：已移除登录鉴权（无 JWT / 密钥），API 对同一网络的
  任何客户端开放。**不建议**不做任何反代/HTTPS 保护就暴露到公网。
- 保留的上传文件安全：上传大小硬上限、zip 解压路径穿越/炸弹防护、workshop_id 数字
  校验、配置路径黑名单、Skymods SSRF 白名单、Mod 设置 XML 大小与格式校验。
- **Skymods 默认关闭**：需在配置中显式设置 `enable_skymods = true`；首次失败后熔断
  10 分钟，`/api/status` 的 `sources.skymods` 会显示 `disabled` 或 `blocked`。
- **ModsConfig 写入守卫**：所有写入只走 `profile.modsconfig_target` 与显式
  `Config.config_dir`，不再隐式扫描 LocalLow。`RWMOD_TEST_SANDBOX_HOME` 是**测试专用**
  的沙箱守卫（越界即 fail-closed），不是生产环境开关。
- 安全漏洞请通过 [SECURITY.md](SECURITY.md) 报告的流程私信提交。

## 📝 License

MIT（详见 [LICENSE](LICENSE)）
