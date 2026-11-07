# 前端测试与 `data-testid` 规范

本目录是 rwmod 前端的测试网：Playwright E2E 钉住**用户可见业务流程**，
Vitest 覆盖组件层与共享工具。

## 目录

```
tests/
  fixtures/backend.ts   # 启动真实 uvicorn：随机端口 + 临时 HOME + 空配置
  e2e/helpers.ts        # worker 级 backend fixture + gotoPanel/expectToast
  e2e/*.spec.ts         # 15 个 panel 的核心流程 + 导航 / 命令面板
  unit/setup.ts         # Vitest + happy-dom + MSW 骨架
  unit/*.test.ts(x)     # 组件层 / 共享工具单测
```

## 运行

```bash
cd frontend
npm test            # Vitest（happy-dom + MSW）
npm run build       # 生成 ../static（E2E 依赖真实构建产物）
npm run test:e2e    # Playwright（自动启动真实后端）
```

E2E 不 mock 前端：`npm run build` 的产物由真实 FastAPI 静态挂载提供。
仅对会触发真实 SteamCMD / Steam Workshop 网络请求的端点使用 `page.route`
（下载 SSE、合集预览、导入、搜索、检查更新），并在测试名标注 `backend mocked`。

### 单一 React 入口（2026-11-07 cutover 后）

旧 Vanilla 入口已删除；React 19 shell 是唯一入口，由 `/` 提供，路由是 hash
（`/#<panel>`）。所有 spec 都直接跑 React 入口，没有入口开关。

约定：

1. **spec 里禁止硬编码入口 URL**。导航统一走 `helpers.ts` 的
   `entryURL(backend, panel?)` / `gotoPanel(page, backend, panel)`；导航控件统一走
   `navControl(page, panel)`（React 侧栏 `nav-<name>` testid）。
2. `react-*.spec.ts` 是 React 专属回归（显式访问 `/`），也属于同一入口的补充覆盖。

## 隔离保证

- 后端 HOME / USERPROFILE / `RWMOD_TEST_SANDBOX_HOME` 指向 `%TEMP%/rwmod-e2e-*`；
- `.rwmod.toml`、SQLite、日志、profiles、Mod 设置备份全部落在该临时目录；
- Mods / RimWorld / 备份目录均为空目录，不会扫描或改动真实游戏目录；
- 测试结束 `taskkill` 进程树并删除临时 HOME（尽力而为）。

## `data-testid` 命名规范（React 重写必须沿用）

统一小写 kebab-case，**按角色而非实现**命名；禁止用文案作为选择器锚点
（文案会变，testid 不应变）。

| 规则 | 用途 | 示例 |
|---|---|---|
| `panel-<name>` | 顶层面板容器 | `panel-dashboard`、`panel-config` |
| `nav-<name>` | 导航入口（tab / sidebar） | `nav-mods`、`nav-backups` |
| `action-<verb>` | 用户动作按钮 | `action-save`、`action-refresh`、`action-start-queue` |
| `field-<name>` | 表单输入 | `field-mods-dir`、`field-mod-input` |
| `item-<kind>-<id>` | 重复列表项 | `item-mod-123456789`、`item-backup-<filename>` |
| `list-<name>` | 列表容器 | `list-mods`、`list-backups` |
| `status-<name>` | 状态 / 提示区域 | `status-config`、`status-toast` |
| `dialog-<name>` | 模态 / 浮层 | `dialog-command-palette` |

约定：

1. `<name>` 使用注册表里的 canonical 名称（`panel-registry.ts`），不用别名
   （`updates` 是 `queue` 的别名，testid 必须写 `panel-queue`）。
2. `<id>` 优先用业务标识（Workshop ID、packageId、profile 名），没有稳定
   标识时才用数组下标，且必须同时存在稳定父容器。
3. 同一元素只挂一个 testid；不要在 testid 里编码状态（如 `-active`），
   状态用 `data-active` / ARIA 表达。
4. 交互元素必须有可访问名称（`aria-label` 或可见文字），testid 不是无障碍替代。
5. 新增 testid 时同步更新本表；删除类名前先全局搜索 `getByTestId`。

## 覆盖清单（wave 0a）

| Panel | 测试文件 | 核心流程 |
|---|---|---|
| dashboard | `e2e/dashboard.spec.ts` | 零数据统计、空活动、快捷跳转 |
| download | `e2e/download.spec.ts` | SSE 日志/完成事件、依赖预览（下载端点 mock） |
| collection | `e2e/collection.spec.ts` | 合集预览计数（预览端点 mock） |
| import | `e2e/import.spec.ts` | .txt / ModsConfig.xml 导入结果（上传端点 mock） |
| mods | `e2e/mods.spec.ts` | 空安装列表、批量操作校验 |
| search | `e2e/search.spec.ts` | 搜索结果渲染、加入队列（搜索端点 mock） |
| queue | `e2e/queue.spec.ts` | 待处理行、移除、更新检查 changelog（更新端点 mock） |
| rimsort | `e2e/rimsort.spec.ts` | 生成 ModsConfig、对比视图、排序分析 |
| profiles | `e2e/profiles.spec.ts` | 保存 / 删除 profile |
| history | `e2e/history.spec.ts` | 空历史、统计、清空 |
| backups | `e2e/backups.spec.ts` | 空状态、分组列表、版本展开 |
| saves | `e2e/saves.spec.ts` | 空扫描、上传 .rws 解析 |
| tags | `e2e/tags.spec.ts` | 标签列表与文件夹详情；React 入口下创建/重命名/删除 |
| config | `e2e/config.spec.ts` | 读取 / 保存、实例创建切换删除 |
| settings | `e2e/settings.spec.ts` | Mod_*.xml 列表、编辑保存、备份 |

跨面板：`e2e/navigation.spec.ts`（15 面板切换 / hash / 前进后退 / 别名）、
`e2e/command-palette.spec.ts`（Ctrl+K 过滤 / 方向键 / 回车 / Esc）。

## 已知缺口（不假绿）

- **标签创建/重命名/删除**：React `TagsPanel` 提供完整 UI（`field-tag-folder` /
  `field-tag-name` / `action-tag-add` / `field-tag-rename` / `action-tag-rename` /
  `action-tag-delete` / `dialog-tag-delete`）；`tags.spec.ts` 的 CRUD 用例真跑，不 skip。
- **真实下载 / 合集下载 / 全部更新**：会启动 SteamCMD 或访问 Steam Workshop，
  按任务书禁止真实执行；对应 E2E 使用 `page.route` mock 网络层。

## 交互载体（cutover 后）

旧入口的原生 `prompt()` / `confirm()` 分支已随 Vanilla 前端删除，spec 只驱动 React
应用内对话框：

| 流程 | React 入口 |
|---|---|
| config 删除实例 | `dialog-instance-delete` 确认 |
| profiles 保存 | `dialog-profile-save` + `field-profile-name` + `action-profile-save-confirm` |
| profiles 删除 | `dialog-profile-confirm` 确认「删除」 |
| profiles 取消保存 | 打开 `dialog-profile-save` → 点「取消」→ 断言 dialog 关闭、无新增、无成功 toast |
| download ok 日志 | `下载成功`（`✓` 是装饰前缀，断言事件文本与 SSE 顺序） |

实测（2026-11-07 cutover）：**82 passed / 0 failed / 0 skipped**。
