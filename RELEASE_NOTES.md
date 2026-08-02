# rwmod v0.4.3 发布说明

> 全离线 RimWorld Mod 管理器 — 无需 Steam 客户端即可匿名下载、更新、备份与管理 Mod。
> 本版本汇总了 **0.4.1 → 0.4.3** 之间累积的全部改动。

---

## ✨ 新功能

- **📦 一键迁移** — 将配置档案、备份、标签与设置打包为 `.rwmod` 文件，可跨机器一键导出/导入（配置面板 → 一键迁移）
- **↩️ 操作撤销** — 破坏性操作（排序/恢复）前自动快照 `ModsConfig.xml`，可一键回滚（配置档案面板 → 操作撤销）
- **🔌 WebSocket 实时队列广播** — 队列状态变化通过 `/ws` 实时推送到前端，队列面板无需手动刷新
- **🛡 备份回滚** — 更新前自动备份旧版，支持一键恢复
- **📊 健康检查** — Mod 活跃度（维持/停更/废弃/下架）
- **✅ 兼容检测** — 对比 RimWorld 版本，标出不兼容 Mod
- **📐 排序分析** — Harmony 位置、Core/DLC 顺序、已知冲突检测
- **💿 配置档案** — 保存/切换 ModsConfig.xml 快照
- **📤 合集导出** — 从已安装 Mod 反向生成 Workshop ID 列表

## 🔒 安全修复 (P1)

- **认证默认失效修复** — 未配置密钥时所有 API 完全无鉴权，现强制启用认证
- **备份接口路径穿越防护** — 拒绝 `../`、子目录等非法路径，zip 解压前校验成员路径
- **SteamCMD 超时失效修复** — 卡死进程 10 分钟后被真正终止，不再永久挂起
- **Skymods 解压同样启用 zip 路径穿越防护**
- **config API 脱敏** — 不再向前端返回 `steam_api_key`，改为 `has_steam_api_key`

## 🛠 稳定性 / 功能修复 (P2)

- **合集下载彻底修复** — 改用专用端点 `GetCollectionDetails`/`GetPublishedFileDetails`，合集识别与依赖检测真正生效
- **合集 ID 不再遗漏** — 「API + 精确 HTML 爬取」双源合并（并集），任一来源的子项都不丢
- **合集预览接口异常兜底 + 性能优化** — 不再 500，593 个子项由全目录扫描改为一次元数据扫描 + 一次历史查询
- **SSE 实时推送卡死修复** — 改为带心跳的 `asyncio` 事件循环
- **WebSocket 队列推送链路打通** — 队列状态实时广播到前端
- **下载队列并发竞态修复** — 运行中新增的待下载项不再被静默丢弃
- **WebSocket 广播解阻塞** — 慢/卡死客户端不再阻塞事件循环
- **异步端点不再阻塞事件循环** — 下载/导入/流式下载统一用 `asyncio.to_thread`，下载期间其他 API 不再无响应
- **Profile 重命名崩溃修复** — 带 `.xml` 后缀的路径解析错误修复
- **磁盘统计低估修复** — 递归统计子目录大小
- **SQLite 写并发锁** — 多线程写库不再触发 `database is locked`（含 `PRAGMA busy_timeout=5000`）
- **备份恢复原子性** — 先写临时目录再原子替换，中途失败不丢数据
- **依赖 BFS 与健康检查误判** — 依赖遍历保证顺序，Core 不再误判为缺失
- **备份恢复目录匹配修复** — 含 `:`/`?` 等非法字符的 mod 目录恢复失败修复
- **Skymods 重定向递归限制** — 加深度上限，避免 A→B→A 循环栈溢出
- **ModsConfig 对比误报** — 内置 Core 不再误算为 extra
- **Docker 只读挂载修复** — Mods 目录可正常写入
- **移除首页自动触发更新检查** — 仅由「一键更新」按钮显式触发
- **队列取消语义** — 取消下载中的项目后状态保持 cancelled，不再被改回 done/failed
- **`Config.validate()` 放宽** — 仅要求 SteamCMD 存在；RimWorld 游戏目录为可选

## 🧹 清理 (P3)

- **前端接入 hash 路由** — 支持 `#saves`/`#tags` 深链接与浏览器前进/后退
- **缓存隔离** — mods/dashboard 缓存按 `mods_dir` 区分，切换目录不再串数据
- **仓库更名** — 更名为 `rwmod-rimworld-mod-manager`，同步更新 CI 徽章与项目链接
- 测试断言更新至 0.4.3，全套 **223 项测试通过**（覆盖率 49% → 56%）
- mypy 0 错误，ruff 全量格式化，dead code 清理
- CI — bandit 使用 `-ll` 作为硬性门禁

---

## 📦 安装简介

### 方式一：Windows 安装包（推荐）
1. 下载本 Release 的 `rwmod-setup.exe` 安装程序
2. 双击运行，按向导完成安装
3. 启动后浏览器打开 **http://localhost:8000** 即可使用

### 方式二：源码运行
```bash
# 1. 安装依赖（需 Python 3.13+ 与 uv）
uv sync

# 2. 构建前端（需 bun 或 npm）
cd frontend && bun install && bun run build && cd ..
# 或：cd frontend && npm install && npm run build && cd ..

# 3. 启动服务
uv run uvicorn rwmod.server:app --host 0.0.0.0 --port 8000
# 或直接运行 start.bat
```

### 方式三：Docker
```bash
docker build -t rwmod .
docker run -p 8000:8000 -v /path/to/mods:/mods rwmod
```

### 升级说明
- 直接覆盖安装即可，配置与数据自动保留
- 首次启动会自动创建数据库与默认配置

### 系统要求
- Windows 10/11（或 Linux/macOS 源码运行）
- Python 3.13+（源码方式）
- 需安装 [SteamCMD](https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip) 用于匿名下载（可选，Skymods 备用源无需）

---

## 完整变更日志

见 [CHANGELOG.md](CHANGELOG.md)
