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
| 🛡 **安全加固** | 备份/解压路径穿越防护、Steam API Key 脱敏、进程超时保护 |
| 🧩 **依赖管理** | 下载前预览依赖树，自动补装缺失依赖 |
| 💾 **备份回滚** | 更新前自动备份旧版，一键恢复 |
| 📊 **健康检查** | Mod 活跃度（维持/停更/废弃/下架） |
| ✅ **兼容检测** | 对比 RimWorld 版本，标出不兼容 Mod |
| 📐 **排序分析** | Harmony 位置、Core/DLC 顺序、已知冲突检测 |
| 💿 **配置档案** | 保存/切换 ModsConfig.xml 快照 |
| 📤 **合集导出** | 从已安装 Mod 反向生成 Workshop ID 列表 |
| 📦 **一键迁移** | 导出/导入 `.rwmod` 包（profiles/备份/标签/配置），跨机器迁移 |
| ↩️ **操作撤销** | 破坏性操作（排序/恢复）前自动快照 ModsConfig.xml，一键回滚 |
## 一、本次新增功能清单

### ✨ 新功能（2 项）

1. __📦 一键迁移__ — 配置面板新增「一键迁移」卡片

   - __导出__：把配置档案、备份、标签、设置打包为单个 `.rwmod` 文件，可勾选是否包含备份
   - __导入__：上传 `.rwmod` 文件，一键恢复全部设置
   - 后端接口：`POST /api/transfer/export`、`POST /api/transfer/import`
   - 适用场景：换电脑、重装系统、跨机器迁移

2. __↩️ 操作撤销__ — 配置档案面板新增「操作撤销」卡片

   - 破坏性操作（排序、恢复 Profile）前自动快照 `ModsConfig.xml`
   - 可查看最近快照，一键回滚到操作前状态
   - 后端接口：`GET /api/undo`（列表）、`POST /api/undo`（回滚）

### 🔒 安全修复（P1）

- __全新鉴权模型__ — 本机（localhost）免密钥直接可用，局域网客户端需密钥（持久化于 `~/.rwmod.secret`）；设置 `RWMOD_SECRET` 可启用严格模式
- __workshop_id 校验链__ — 全链路强制数字校验，堵住备份任意写盘 / restore 目录穿越 / SteamCMD 命令注入
- __Skymods SSRF 防护__ — 域名白名单 + 私网 IP 拒绝 + 响应大小上限
- __zip 炸弹防护__ — 解压成员数 / 总量 / 重复路径上限，上传端点统一限大小
- __备份接口路径穿越防护__ — 拒绝 `../`、子目录等非法路径，zip 解压前校验成员路径
- __SteamCMD 超时失效修复__ — 卡死进程 10 分钟后被真正终止

### 🛠 稳定性修复（P2）

- SSE 实时推送卡死修复（带心跳的事件循环）
- WebSocket 队列推送链路打通（状态实时广播）
- 下载队列并发竞态修复（运行中新增项不再被丢弃）
- WebSocket 广播解阻塞（慢客户端不再卡住事件循环）
- Profile 重命名崩溃修复（`.xml` 后缀路径）
- 磁盘统计低估修复（递归统计子目录）
- SQLite 写并发锁（不再触发 `database is locked`）
- 备份恢复原子性（先写临时目录再替换）
- 依赖 BFS 与健康检查误判修复（Core 不再误判缺失）
- 备份恢复目录匹配修复（含 `:`/`?` 的 mod 目录）
- Skymods 重定向递归限制（加 5 层深度上限）
- ModsConfig 对比误报修复（内置 Core 不误算 extra）
- Docker 只读挂载修复（Mods 目录可写）

### 🧹 清理（P3）

- 测试断言更新至 0.4.5，全套 __223 项测试通过__
- mypy 0 错误，ruff 全量格式化，dead code 清理

## 🚀 快速开始

```bash
# 安装依赖
uv sync
cd frontend && bun install && bun run build && cd ..
# 或使用 npm：cd frontend && npm install && npm run build && cd ..
#（bun.lock / package.json 二者皆可，Docker 构建使用 npm）

# 启动服务
uv run uvicorn rwmod.server:app --host 0.0.0.0 --port 8000
# 或直接运行
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

## 🏗 架构

```
src/rwmod/
├── server.py           # FastAPI app factory (85行)
├── deps.py             # 依赖注入 (Config/DB/Queue)
├── errors.py           # 统一异常体系
├── routers/            # 19 个路由模块（全部 /api 前缀）
│   ├── auth.py         # 登录 / token 校验
│   ├── auto_update.py  # 自动更新
│   ├── backups.py      # 备份管理
│   ├── compat.py       # 兼容性检查
│   ├── config.py       # 配置管理
│   ├── dashboard.py    # 首页统计
│   ├── download.py     # 下载 / 导入 / SSE 流
│   ├── health.py       # 状态检测
│   ├── history.py      # 下载历史
│   ├── metrics.py      # Prometheus 指标
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
- **Skymods (smods.ru) 备用源**：仅当 SteamCMD 下载失败时作为回退，抓取行为受第三方
  站点条款约束，内容版权归原作者。
- 本项目与 Ludeon Studios / Valve 无任何关联。RimWorld 为 Ludeon Studios 商标。

## 🔒 安全说明

- 默认仅监听 localhost；局域网访问需密钥（见「快速开始」）。**不建议**不做任何
  反代/HTTPS 保护就暴露到公网。
- 安全漏洞请通过 [SECURITY.md](SECURITY.md) 报告的流程私信提交。

## 📝 License

MIT（详见 [LICENSE](LICENSE)）
