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

## 📝 License

MIT
