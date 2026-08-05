# rwmod v0.4.5 发布说明

> 全离线 RimWorld Mod 管理器 — 无需 Steam 客户端即可匿名下载、更新、备份与管理 Mod。
> 本版本在 0.4.4 基础上完成了**一轮全面代码审查与修复**（安全、并发、前端），并汇总
> **0.4.1 → 0.4.5** 之间的累积改动（含 0.4.3 与 0.4.4 合并发布）。

---

## ✨ 新功能

- **📦 一键迁移** — 将配置档案、备份、标签与设置打包为 `.rwmod` 文件，可跨机器一键导出/导入（配置面板 → 一键迁移）
- **↩️ 操作撤销** — 破坏性操作（排序/恢复）前自动快照 `ModsConfig.xml`，可一键回滚（配置档案面板 → 操作撤销）
- **🔌 WebSocket 实时队列广播** — 队列状态变化通过 `/ws` 实时推送到前端，队列面板无需手动刷新
- **� 存档文件解析** — 上传 `.rws` 存档文件，自动检测缺少哪些 Mod
- **🏷 Mod 标签分组** — 自定义标签、按标签筛选 Mod
- **🎯 合集 3 层降级拉取** — Steam Web API → 网页爬取 → 用户自定义 API Key，支持翻页，大合集（500+ Mod）不再丢项
- **�🛡 备份回滚** — 更新前自动备份旧版，支持一键恢复
- **📊 健康检查** — Mod 活跃度（维持/停更/废弃/下架）
- **✅ 兼容检测** — 对比 RimWorld 版本，标出不兼容 Mod
- **📐 排序分析** — Harmony 位置、Core/DLC 顺序、已知冲突检测
- **💿 配置档案** — 保存/切换 ModsConfig.xml 快照
- **📤 合集导出** — 从已安装 Mod 反向生成 Workshop ID 列表

## 🔒 安全修复 (P1)

- **全新鉴权模型（重写）** — 取消公开默认密钥。本机访问（localhost）**免密钥直接可用**——本机用户本就能直接读写 Mods 目录，API 权限不更大；只有**局域网**客户端需要密钥。设置强 `RWMOD_SECRET` 环境变量可启用严格模式（本机也要求登录）
- **认证失效根因修复** — `get_current_user` 缺 `return user` 导致 `/api/auth/verify` 返回 `user: null`、前端显示 `undefined`；已修复
- **登录死锁修复** — 未配置 `RWMOD_SECRET` 时自动生成随机密钥并**持久化到 `~/.rwmod.secret`**（跨重启有效），登录采用恒定时间比较 + 失败限速（10 次/5 分钟）
- **workshop_id 校验链修复** — `PublishedFileId.txt`/URL 参数读取的 workshop_id 全部强制 `^\d+$`，堵住：备份 zip 写出 backup_dir（任意文件创建）、restore 目录穿越、SteamCMD 命令 token 注入三条路径
- **Skymods SSRF 防护** — 下载 URL 强制白名单域名（smods.ru/modsbase.com）+ 拒绝私网/回环/云元数据 IP + 响应/页面大小上限
- **zip 炸弹防护** — `safe_extract_zip` 增加成员数上限、解压总大小上限（4GB）、重复路径校验；全部上传端点统一大小上限
- **鉴权收紧** — `/ws` WebSocket、`/api/onboarding/check`、`/api/metrics` 现在需要认证（本机免密钥策略下本机仍免登录）
- **备份接口路径穿越防护** — 拒绝 `../`、子目录等非法路径，zip 解压前校验成员路径
- **SteamCMD 超时失效修复** — 卡死进程 10 分钟后被真正终止，不再永久挂起
- **Skymods 解压同样启用 zip 路径穿越防护**
- **config API 脱敏** — 不再向前端返回 `steam_api_key`，改为 `has_steam_api_key`

## 🛠 稳定性 / 功能修复 (P2)

- **下载队列 worker 崩溃修复** — 一次磁盘满/权限错误不再杀死常驻 worker，失败项标记 `failed` 后继续
- **下载锁死锁修复** — per-mod 锁在依赖/合集子项上改为「锁外收集后下载」，环形依赖不再互等导致队列永久停摆
- **`_find_existing` 快路径修复** — 数据库查询返回值与调用方类型不匹配，导致快路径每次抛异常回退全目录扫描；现已真正生效
- **数据库连接生命周期修复** — `init_db` 重入 `_init_lock` 死锁（首次启动卡死）+ `close_db` 后其他线程拿到已关闭连接报错；均已修复
- **SSE 心跳** — 下载流每 30s 发心跳，nginx 反代 60s 超时不再中途掐断；客户端断线语义修正（不提前释放信号量）
- **WebSocket 背压 + 鉴权** — 慢/卡死客户端超出发送积压（200 条）即断开，不再无界缓冲；连接需 `?token=`
- **WebSocket 广播任务引用** — 修复 `Task was destroyed but it is pending` 通知丢失
- **合集 SSE 性能** — 500+ 子项的一次性目录映射替代逐项全目录扫描
- **事件循环阻塞清理** — dashboard 磁盘统计/健康检查、import-local 解压、`_find_existing` 全部移入 `to_thread`；mods 列表缓存 TTL 3s→30s
- **批量下载并发上限** — `/api/mods/batch/download` 复用全局信号量，与队列/SSE 叠加不再超 SteamCMD 并发预算
- **备份恢复原子交换** — rmtree→rename 改为 rename 交换 + 失败回滚，RimWorld 锁文件时不再毁 mod
- **Profile/RimSort 原子写** — ModsConfig.xml 先写临时文件再替换，写一半崩溃不再留截断 XML
- **force 覆盖失败自动回滚** — 覆盖下载失败时自动从备份恢复旧版
- **transfer 导出临时文件** — 唯一文件名 + 响应完成后自动清理，不再泄漏 `%TEMP%`/同秒覆盖
- **合集下载彻底修复** — 改用专用端点 `GetCollectionDetails`/`GetPublishedFileDetails`，合集识别与依赖检测真正生效
- **合集 ID 不再遗漏** — 「API + 精确 HTML 爬取」双源合并（并集），任一来源的子项都不丢
- **合集预览接口异常兜底 + 性能优化** — 不再 500，593 个子项由全目录扫描改为一次元数据扫描 + 一次历史查询
- **SSE 实时推送卡死修复** — 改为带心跳的 `asyncio` 事件循环
- **异步端点不再阻塞事件循环** — 下载/导入/流式下载统一用 `asyncio.to_thread`，下载期间其他 API 不再无响应
- **下载队列并发竞态修复** — 运行中新增的待下载项不再被静默丢弃
- **WebSocket 广播解阻塞** — 慢/卡死客户端不再阻塞事件循环
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

## 🎨 前端修复

- **认证流程重写** — 删除硬编码默认密钥；本机打开直接进主界面，登录框仅局域网场景弹出（提示密钥见 `~/.rwmod.secret` 或启动日志）
- **401 统一拦截** — token 失效自动清 token + 弹登录框，不再各面板显示 `undefined`
- **SSE 下载流** — 检查 `resp.ok` + 2 分钟超时兜底，失败不再永久挂起
- **面板补齐** — Saves/Tags 面板 HTML 骨架、RimSort 拖拽/生成、History 统计/自动更新区（原缺失导致按钮静默失效）
- **队列"检查更新/全部更新"** — 按钮绑定修复，不再永不触发
- **WS 断线 REST 轮询兜底** — 重连失败后每 5s 轮询 `/api/queue`，恢复后自动切回
- **轮询 interval 泄漏** — 队列为空/切面板时清理
- **undefined 修复** — dashboard 一键更新、mods 兼容摘要、错误 toast 不再显示 `undefined`
- **XSS 转义** — queue/collection 面板模板拼接改用 `esc()`

## 📦 打包 / 体积优化

- **安装包体积大幅优化（0.4.4）** — 修复 steamcmd 目录误打包 1.4GB Mod 缓存的问题，并排除 torch/cv2/pandas 等无关 AI 库；安装包从 1.4GB+ 降至 **114 MB**
- **全新 PyInstaller + Inno Setup 安装包（0.4.1）** — 单文件 EXE + 内置 SteamCMD + 桌面快捷方式

## 🧹 清理 (P3)

- **前端接入 hash 路由** — 支持 `#saves`/`#tags` 深链接与浏览器前进/后退
- **缓存隔离** — mods/dashboard 缓存按 `mods_dir` 区分，切换目录不再串数据
- **仓库更名** — 更名为 `rwmod-rimworld-mod-manager`，同步更新 CI 徽章与项目链接
- 测试断言更新至 0.4.5，全套 **223 项测试通过**（覆盖率 49% → 56%）
- mypy 0 错误，ruff 全量格式化，dead code 清理
- CI — bandit 使用 `-ll` 作为硬性门禁

---

## 📦 安装简介

### 方式一：Windows 安装包（推荐）
1. 下载本 Release 的 `rwmod_setup.exe` 安装程序
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
- Windows 10/11（安装包方式）
- Python 3.13+（源码方式）
- **无需安装 SteamCMD** — 安装包已内置 SteamCMD，开箱即用；源码方式在设置中指定路径即可（或留空走 Skymods 备用源）

---

## 完整变更日志

见 [CHANGELOG.md](CHANGELOG.md)
