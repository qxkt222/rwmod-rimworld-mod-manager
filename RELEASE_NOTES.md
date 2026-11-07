# rwmod v0.8.0 发布说明

> 全离线 RimWorld Mod 管理器 — 无需 Steam 客户端即可匿名下载、更新、备份与管理 Mod。
> 本次是重构版本：前端从 Vanilla TypeScript 全量重写为 React 19，后端清掉 0.7.0 审查
> 遗留的债务，打包体积下降 41%。定位于个人本机 / 局域网自用，**不是公网软件**，
> 请勿直接暴露到公网。

## 🎨 前端重写（React 19）

- **15 个面板全部 React 化**：状态走 Zustand 切片 + revision bus，取代每个面板手动刷新。
- **导航重做**：顶部 15 个平铺 tab 与侧栏重复，现统一为左侧栏 5 组
  （概览 / 获取 / 整理 / 数据 / 系统）。
- **自写 hash router**：零运行时依赖，修复 `#updates` 别名地址栏不回写的问题。
- **API 类型自动生成**：从 FastAPI OpenAPI 生成 TypeScript 类型（31 → 78 schemas），
  不再手写 40 个 interface 与后端漂移。
- **危险操作改应用内弹窗**：实例删除、档案保存/删除、标签删除不再用原生 `prompt/confirm`。
- **标签管理补 UI**：创建 / 重命名 / 删除（此前只有只读列表，接口早已存在）。
- **CSP**：HTML 文档响应加 `Content-Security-Policy`（`script-src 'self'`），
  `connect-src` 放行 WebSocket。

## 🛡 后端

- **安全**：堵住 steamcmd 任意可执行链（`mods_dir` 植入也会被 `Config.validate()` 拦截）、
  2GB zip 全量入内存、分块上传绕过上限、WebSocket Origin 未校验、JSON body 无上限。
- **失败契约统一**：失败抛 `errors.py` 异常，移除 `200 + {ok:false}`；高频端点补
  `response_model`。
- **多实例隔离**：队列、数据库、标签、备份、Mod 设置全部按 `(instance_id, id)` 隔离。
  取消下载不再误杀另一实例的同 ID 任务。
- **原子写**：ModsConfig / profile / undo 全部改 `mkstemp` + `os.replace`，消除截断窗口。
- **下载内核**：stash 失败不再裸删旧版；取消检查点前置到等待锁 / 探测阶段；
  安装锁改为按目标文件夹；Steam API 失败不再伪装「全部最新」。
- **更新检查支持部分失败**：返回 `partial` + 失败批次数，不再因为一批网络抖动整体报错。
- **合集预览**：区分「不可达 502」与「确认空 404」，不再把网络失败说成合集不存在。
- **BOM 直传**：RimSort 写回不再经过字符串层，BOM 只出现一次。

## 📦 打包

- **exe 82.5 → 51.9 MB**；安装包 **121 → 70.7 MB**（排除 SteamCMD 自更新缓存）。
- 版本号统一到 `pyproject.toml`，移除执行即报错的 `rwmod-web` 入口。
- Dockerfile 改用 `package-lock.json` + `uv.lock`，构建可复现。
- 卸载时清理 `steamapps`，不再残留数 GB。

## 🧪 测试

- 后端 **1496 passed / 0 failed**（0.7.0 为 1388 + 2 红），覆盖率 91.7%（阈值 90）。
- 前端 **31 个单元测试 + 82 条 Playwright E2E**（真实后端、临时 HOME、随机端口），
  此前前端测试数为 0。
- CI 新增 Windows 打包 job（PyInstaller + exe 探活 + Inno Setup）与 Windows pytest。

## ⚠️ 已知限制

- 安装包体积主要来自内置 SteamCMD，与业务代码无关。
- 真实 SteamCMD 下载 / Workshop 搜索在 CI 中为 mock，未做真实网络压测。
- Docker 镜像未在本机实跑（Hypervisor 未启用），`docker compose config` 已校验。

---

**下载**：`rwmod_setup.exe`（安装版）或 `rwmod.exe`（免安装）。
