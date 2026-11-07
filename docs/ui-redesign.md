# rwmod 前端交互重做方案（React 19）

> 状态：已落地（2026-11-07 cutover；React 是唯一前端入口）。
> 依据：`frontend/tests/README.md`（testid 规范）、`frontend/tests/e2e/*.spec.ts`（E2E 基线）、
> `frontend/src/panel-registry.ts`（15 个 canonical panel）。
> 约束：React 19 + Vite 8 + Zustand + 手写 CSS；不引 UI 组件库；不新增运行时依赖（React 生态除外）。

---

## 0. 不变量

1. **Canonical panel 仍是 15 个**，名字以 `panel-registry.ts` 为准；`updates` 永远是 `queue` 的别名，
   不新增 panel。
2. **testid 按 `frontend/tests/README.md` 输出**：`panel-<name>` / `nav-<name>` / `action-<verb>` /
   `field-<name>` / `item-<kind>-<id>` / `list-<name>` / `status-<name>` / `dialog-<name>`，
   状态用 `data-active` / ARIA 表达，不写进 testid。
3. **过渡期保留旧 E2E 锚点**。现有 54 条 spec 主要用旧 DOM id（`#mod-list`、`#btn-queue-start`
   …）和 `#panel-<name>.active`，不是 testid。React 组件在 wave 2/3 必须**同时**输出旧 id/class
   与规范 testid，否则 `test:e2e` 立刻回归。等 FE-TEST 把 spec 迁到 testid 后再删旧锚点（见 §6）。
4. **URL 仍是 `#<panel>`**：深链接、前进后退、`#updates` 别名全部保留。
5. **面板不直接调 API**：组件只 dispatch store action，读写统一走 `src/api/client.ts`。

---

## 1. 信息架构：15 个 panel 的分组与导航

### 1.1 现状问题

今天顶部是 15 个平铺 tab，左侧栏又把同样的 15 项分 4 组重列一遍；两套导航没有共同的信息层级，
宽度 1440px 时顶部 tab 已接近溢出，且「下载 / 合集 / 导入 / 队列」这些同一工作流的入口被首页、
Mods、搜索隔开。

### 1.2 新分组（单一左侧栏，5 组）

| 组 | 组内顺序 | Panel（canonical） | 中文标签 | 职责 |
|---|---|---|---|---|
| 概览 | 1 | `dashboard` | 首页 | 统计、最近活动、快捷操作 |
| 获取 | 2 | `search` | 搜索 Workshop | 关键词搜索、加入队列 |
| 获取 | 3 | `download` | 下载 Mod | 单 Mod 下载 + SSE 日志 |
| 获取 | 4 | `collection` | 下载合集 | 合集预览/批量下载 |
| 获取 | 5 | `import` | 导入列表 | .txt / ModsConfig.xml 导入 |
| 获取 | 6 | `queue` | 下载队列 | 队列控制 + 更新检查（原 `updates`） |
| 整理 | 7 | `mods` | 已安装 Mods | 列表、启用/禁用、批量选择 |
| 整理 | 8 | `rimsort` | RimSort | ModsConfig 生成、对比、排序分析 |
| 整理 | 9 | `profiles` | 配置档案 | 档案保存/删除、撤销 |
| 整理 | 10 | `tags` | 标签 | 标签列表与文件夹详情 |
| 数据 | 11 | `history` | 下载历史 | 历史列表、统计、自动更新 |
| 数据 | 12 | `backups` | 备份 | 备份分组、版本展开、清理 |
| 数据 | 13 | `saves` | 存档分析 | 存档扫描、.rws 上传 |
| 系统 | 14 | `config` | 配置 | 路径/实例/迁移 |
| 系统 | 15 | `settings` | Mod 设置 | Mod_*.xml 列表/编辑/备份 |

### 1.3 布局

```
┌──────────────────────────────────────────────────────────────┐
│ Topbar: [☰] 整理 / 已安装 Mods      ● 实时  🟢 Steam  🌙 ⚡ 🔄 │
├──────────────┬───────────────────────────────────────────────┤
│ Sidebar      │ PanelHost                                     │
│ 概览          │  <DashboardPanel />                           │
│  首页         │                                               │
│ 获取          │                                               │
│  搜索 下载 …  │                                               │
│ …            │                                               │
└──────────────┴───────────────────────────────────────────────┘
```

- **Sidebar** 固定 220px；< 900px 收成 56px 图标栏；< 700px 变抽屉（Topbar 汉堡键）。
  分组标题不可点击；组内项是 `<a href="#panel">`，获得原生键盘/中键/右键行为。
- **Topbar** 只放全局能力：面包屑（组 / 面板）、WS 实时状态点（断线时变灰并带 tooltip，
  对应旧 `#queue-count` 的 opacity/title 行为）、Steam 在线指示、主题切换、命令面板、刷新当前面板。
  **删除顶部 15 tab 行**，导航只保留一份。
- 选中态：`aria-current="page"` + `data-active="true"`；testid `nav-<name>`。
- 角标：`mods` 显示已安装数，`queue` 显示 pending+downloading 数；WS 断线时数字置灰并标注
  「数字可能不是最新」（沿用旧行为）。

### 1.4 快捷键（冻结，不随分组改动）

沿用现有映射（`PANEL_SHORTCUTS` 从 `PANEL_NAMES` 派生、跳过 `dashboard`）：

| 键 | Panel | 键 | Panel |
|---|---|---|---|
| Ctrl+1 | download | Ctrl+5 | search |
| Ctrl+2 | collection | Ctrl+6 | queue |
| Ctrl+3 | import | Ctrl+7 | rimsort |
| Ctrl+4 | mods | Ctrl+8 | profiles |

其余面板经命令面板可达（无快捷键角标）。实现时把这张表**显式**写进 `src/router/routes.ts`，
不再依赖「数组顺序」派生——分组一旦重排，隐式派生会把用户的肌肉记忆换掉。
输入框/文本域/下拉/contentEditable 内不拦截按键（沿用 `isTypingTarget` 逻辑）。

---

## 2. 全局状态：Zustand 切片

### 2.1 规则

1. 一个领域一个 slice 文件；`create<Slice>()`，不写 class，不做全局单例以外的东西。
2. **只有 slice action 调 API**；组件通过 `useXxxStore(s => ...)` 订阅，禁止把 store 数据再拷进
   `useState`（避免双份真源）。
3. 跨 panel 共享 → slice；只有本面板用 → `useState`。
4. 派生值用 selector（必要时 `useShallow`），不在 store 里存可计算的冗余字段。
5. 异步 action 统一返回 `Promise<void>`，错误写入 slice 的 `error` 字段并由调用方决定是否 toast。

### 2.2 切片划分

| Slice | 文件 | State | 写入方（owner） | 订阅方 |
|---|---|---|---|---|
| `ui` | `store/ui.ts` | `theme`、`sidebarCollapsed`、`commandPaletteOpen`、`activeDialog`、`toasts[]` | AppShell / Topbar / CommandPalette / ToastViewport / DialogHost | 全部布局组件 |
| `config` | `store/config.ts` | `config`、`instances`、`activeInstanceId`、`steamOnline`、`steamcmdCheck`、`status` | 启动 bootstrap + `ConfigPanel` | Sidebar（环境/角标）、Dashboard、Download、Collection、Mods、Rimsort、Profiles、Saves、Settings、Config |
| `mods` | `store/mods.ts` | `items`、`onlyActive`、`selectedIds`、`health`、`compat`、`status` | `ModsPanel`；下载/导入完成时由 bus 失效 | Dashboard（计数）、Mods、Rimsort、Profiles、Tags、CommandPalette |
| `queue` | `store/queue.ts` | `items`、`running`、`wsState`、`pendingCount` | `wsBridge` + `QueuePanel` | Sidebar 角标、Dashboard、Search、Download、Collection、Import、Queue |
| `updates` | `store/updates.ts` | `checkStatus`、`outdated`、`changelog`、`autoUpdateStatus` | `QueuePanel` 更新区 + `wsBridge` | Dashboard（待更新）、Sidebar 角标、Queue |
| `transfer` | `store/transfer.ts` | `channels: { download, collection, import }`，每项 `lines[]` / `progress` / `streaming` | Download / Collection / Import 的 action 与 SSE 回调 | 对应面板（日志在切走后仍保留） |
| `bus` | `store/bus.ts` | `revision: Record<Topic, number>` | 所有 mutation action | 任意面板（`useRefreshOn(topic, fn)`） |

**留在组件本地**（无跨面板消费者）：搜索词与结果、Rimsort 输出、Saves 扫描结果、Tags 详情选中项、
Settings 编辑器草稿、History 列表的排序/过滤。History 的统计只在本面板展示，也留在本地。

### 2.3 API 客户端

- `src/api/client.ts`：`request<T>(method, path, {body, query, signal})`，基于 `generated.ts` 的
  `paths` 做重载，4xx/5xx 统一抛 `ApiError { status, code?, detail }`。
- `src/api/generated.ts` 由 `npm run gen:api` 生成；**大部分 200 响应当前是 `unknown`**
  （FastAPI 路由没有 `response_model`）。FE-1 在 `src/api/types.ts` 给高频端点补窄化类型，
  同时把「加 `response_model`」列入 BE-A/BE-B 的后端任务——生成器本身没错。
- 旧 `src/api.ts` 一行不动（旧前端继续用）。

---

## 3. 路由选型

### 选项

| 方案 | 优点 | 代价 |
|---|---|---|
| A. `react-router` v7 | 嵌套路由、loader、成熟生态 | 新增运行时依赖（未授权）；15 个扁平面板用不到 loader/嵌套 |
| B. 自写 hash router（约 80 行） | 零依赖；完全掌控别名/未知 hash/替换语义；`useSyncExternalStore` 即可 | 无嵌套路由、无路由级数据加载（本项目也不需要） |

### 结论：选 B（自写 hash router）

理由：路由只有「15 个扁平 panel + 2 个覆盖层（命令面板、弹层）」，没有路径参数、没有嵌套布局、
没有路由级鉴权；唯一特殊需求（`#updates` 别名、未知 hash 回退、初始载入不新增历史记录）
自己写反而更短、更可测。若将来出现 `#mods/<id>` 这类详情路由，在 `useRoute()` 后面加一层
`panel/segment` 匹配即可，接口不变；真需要 loader 时再迁 react-router，成本被 `useRoute()` 隔离。

### 实现

- `src/router/routes.ts`：canonical name → `{ label, group, icon, shortcut }`；`PANEL_ALIASES`；
  `resolvePanel()`。名字常量继续从 `src/panel-registry.ts` 导入（不复制、不改旧文件）。
- `src/router/hashRouter.ts`：
  - `useRoute()`：`useSyncExternalStore(subscribe, getSnapshot)`，snapshot 是解析后的 canonical panel。
  - `navigate(panel, { replace })`：`history.pushState/replaceState` + 本地通知（pushState 不触发事件）。
  - 监听 `hashchange` 与 `popstate`，按「解析后 panel 是否变化」去重，避免一次后退触发两次渲染。
  - **别名归一化**：`#updates` 解析成 `queue` 后用 `replaceState` 把地址栏改成 `#queue`，
    修掉 0a 决策记录 #3（今天运行时跳转后地址栏不更新）。
  - 未知/空 hash → `dashboard`；空 hash 用 `replaceState`（不污染历史），未知 hash 同样替换。
- 面板渲染：`PanelHost` 按 `useRoute().panel` 渲染，`React.lazy` + `<Suspense>` 按面板分包。
  同一时刻只有一个 `panel` 处于 active（`data-active="true"` 且旧 class `active`，见 §0.3）。

---

## 4. 跨 panel 刷新与订阅

### 4.1 取代「每个 panel 自己 refresh」

`store/bus.ts` 维护 `revision[topic]`：

- mutation action 成功后 `bump("mods")` / `bump("history")` …；
- 面板用 `useRefreshOn("mods", fetchMods)`：挂载时读当前 revision，与本地 `lastSeen` 比较；
  不一致就重新拉取，并把 `lastSeen` 更新。
- 结果：**已挂载的面板立即刷新；未挂载的面板上次离开后数据过期，下次进入自动重拉**，
  不需要每个面板手动写刷新按钮逻辑。刷新按钮保留（E2E 依赖），点击等价于 `bump(topic)`。

Topic 清单：`mods`、`queue`、`history`、`backups`、`profiles`、`tags`、`config`、`settings`、
`updates`、`saves`。

典型失效链：

| 事件 | bump |
|---|---|
| 下载/合集/导入完成 | `mods`、`queue`、`history`、`dashboard` |
| 队列增删/清空 | `queue`、`dashboard` |
| 启用/禁用 Mod、批量操作 | `mods`、`rimsort`、`profiles` |
| 保存/删除 profile、撤销 | `profiles`、`mods` |
| 备份创建/恢复/清理 | `backups`、`mods` |
| 标签增删改 | `tags`、`mods` |
| 配置保存/切换实例 | `config`（全局，所有面板重读） |

### 4.2 WebSocket 事件进 store

`src/api/ws.ts`（新文件，旧 `src/ws.ts` 不动）是**单例连接**，重连退避与 REST 轮询兜底沿用旧逻辑
（5 次重连 → 每 5s 拉 `/api/queue`）。消息分发：

| WS `type` | 载荷 | Store action | 副作用 |
|---|---|---|---|
| `queue_update` | `items[]` | `queue.setItems(items)` | 更新 Sidebar 角标 |
| `update_notification` | `count`、`updates[]` | `updates.markOutdated(updates)` | toast「发现 N 个更新」；bump `updates` / `dashboard` |
| `connected`（本地合成） | — | `queue.setWsState("connected")` | 立即 `queue.refresh()` + `updates.refreshStatus()` 对齐断线期间的状态 |
| `disconnected`（本地合成） | — | `queue.setWsState("disconnected")` | 角标置灰 + tooltip |

SSE（下载日志）由 `transfer` slice 的 action 创建 `EventSource`，**不随组件卸载关闭**；
面板卸载只是取消订阅。用户显式「清空日志」或传输结束才 `close()`。

---

## 5. 命令面板 / 快捷键 / Toast / 通知

### 5.1 命令面板

- `components/CommandPalette.tsx`，AppShell 内单例挂载，portal 到 `body`。
- 数据源 `src/app/commands.ts`：panel 命令由 `routes.ts` 生成（label/group/icon/shortcut 单一真源），
  再加动作命令：导出 Mod 列表、切换主题、刷新当前面板、清空下载日志、检查更新。
- 交互：子序列匹配（`下载` 命中三项，顺序保持 下载 Mod → 下载合集 → 下载队列）；
  ArrowUp/Down 移动、Enter 执行、Esc 关闭；焦点陷阱 + 关闭后焦点还给触发元素。
- 兼容锚点（过渡期）：`id="cmd-palette"`、`class="open"`、`#cmd-input`、`.cmd-result`、
  选中 `class="selected"`——现有 `command-palette.spec.ts` 直接依赖这些，FE-1 必须保留到
  FE-TEST 迁移 spec。新 testid：`dialog-command-palette`。

### 5.2 快捷键

`useGlobalShortcuts()` 只注册一次（AppShell）：
Ctrl+K 开/关命令面板；Ctrl+1..8 跳 panel；Esc 关闭最上层覆盖层（面板 → 弹窗 → 命令面板）。
输入目标保护沿用 `isTypingTarget()`。

### 5.3 Toast

- `store/ui.ts` 的 `toasts[]` + `components/ToastViewport.tsx`（固定右下，`#toast-container` 兼容锚点，
  testid `status-toast`）。
- `notify.success/error/warn/info(message, {duration})`；同文案 1s 内去重；最多同屏 5 条；
  点击立即消失；`role="status"` + `aria-live="polite"`。
- API 错误默认 `notify.error(ApiError.detail)`；需要内联展示的（表单校验）由组件自己渲染
  `role="alert"` 区域，不再只弹 toast。

### 5.4 弹窗与原生 prompt

现状 `profiles` 用 `window.prompt` / `window.confirm`，`profiles.spec.ts` 用 `page.on("dialog")`
接住。React 第一版**保留原生 prompt**（否则该 spec 必红）；后续若换成应用内 `Dialog`
（`dialog-*` testid、焦点陷阱），必须与 FE-TEST 同一波改 spec，不能单方面换。

---

## 6. 迁移映射：E2E → 新组件

### 6.1 每个 spec 的承接组件

计数为 0a 基线（共 54 条：53 passed + 1 skipped）。

| Spec | 条数 | 承接组件 | 过渡期必须保留的旧锚点 |
|---|---|---|---|
| `dashboard.spec.ts` | 2 | `panel/DashboardPanel` + `layout/Sidebar`（快捷跳转） | `#db-mods-count`、`#db-updates-count`、`#db-disk-usage`、`#db-activity`、`#queue-count`、`#panel-search`（跳转目标） |
| `download.spec.ts` | 3 | `panel/DownloadPanel` + `store/transfer` | `#mod-input`、`#btn-download`、`#dep-preview`、`#log`、`#btn-clear-log` |
| `collection.spec.ts` | 2 | `panel/CollectionPanel` + `store/transfer` | `#collection-input`、`#btn-collection-preview`、`#log-collection` |
| `import.spec.ts` | 3 | `panel/ImportPanel` + `store/transfer` | `#import-file`、`#import-sort` |
| `mods.spec.ts` | 3 | `panel/ModsPanel` + `store/mods` | `#mod-list`、`#mod-count`、`#mods-only-active`、`#btn-mods-enable`、`#btn-mods-disable` |
| `search.spec.ts` | 3 | `panel/SearchPanel` + `store/queue` | `#search-input`、`#btn-search`、`#search-results` |
| `queue.spec.ts` | 5 | `panel/QueuePanel` + `panel/queue/UpdateSection` + `store/queue`、`store/updates` | `#queue-list`、`#btn-queue-start`、`#btn-queue-pause`、`#btn-queue-clear`、`#btn-check-updates`、`#btn-update-all`、`#update-results` |
| `rimsort.spec.ts` | 3 | `panel/RimsortPanel` | `#btn-rimsort-generate`、`#rimsort-output`、`#rimsort-file`、`#rimsort-compare-result`、`#btn-rimsort-order`、`#rimsort-order-result` |
| `profiles.spec.ts` | 2 | `panel/ProfilesPanel` | `#btn-profile-save`、`#profile-list`、`#profile-modsconfig-path`、`#undo-status`、`.profile-item`、`[data-delete]`、原生 `prompt/confirm` |
| `history.spec.ts` | 3 | `panel/HistoryPanel` | `#history-list`、`#history-stats`、`#btn-history-refresh`、`#btn-clear-history`、`#auto-update-status` |
| `backups.spec.ts` | 3 | `panel/BackupsPanel` | `#backup-list`、`#backup-group-label`、`#btn-backup-refresh`、`#versions-<id>` |
| `saves.spec.ts` | 3 | `panel/SavesPanel` | `#saves-results`、`#btn-scan-saves`、`#saves-file-upload` |
| `tags.spec.ts` | 2 + 1 skip | `panel/TagsPanel` | `#tags-list`、`#tags-detail` |
| `config.spec.ts` | 4 | `panel/ConfigPanel` + `store/config` | `#config-display`、`#cfg-steamcmd`、`#cfg-mods`、`#cfg-rimworld`、`#btn-save-config`、`#instance-list`、`#btn-add-instance`、`#new-instance-*` |
| `settings.spec.ts` | 4 | `panel/SettingsPanel` | `#mod-settings-*`（list/status/editor/xml/ack/backups/editor-title）、`#btn-mod-settings-*` |
| `navigation.spec.ts` | 5 | `layout/AppShell` + `layout/Sidebar` + `router/hashRouter` + `layout/PanelHost` | `#panel-<name>.active`（全部 15 个）、`[data-panel]` 点击、`#updates` 别名 |
| `command-palette.spec.ts` | 3 | `components/CommandPalette` + `layout/Topbar` | `#cmd-palette.open`、`#cmd-input`、`.cmd-result`、`.selected`、`#btn-cmd`、`#panel-collection.active` |

> 结论：**旧锚点不是可选项，是过渡期的兼容层。** 组件输出「旧 id/class + 新 testid」双轨，
> 等 FE-TEST 把 spec 改成 `getByTestId` 与 `data-active` 后，兼容层可一次性删除。

### 6.2 面板 → 依赖

| Panel | 依赖的 store/slice | 依赖的其它面板能力 |
|---|---|---|
| dashboard | config、queue、updates、mods | 跳转 search / download / queue |
| search | queue、mods | 加入队列 |
| download | queue、transfer、config | 依赖预览 |
| collection | queue、transfer | 合集预览 |
| import | queue、transfer | 文件上传 |
| queue | queue、updates | 更新检查 changelog |
| mods | mods、config | 健康/兼容角标 |
| rimsort | mods、config | ModsConfig 生成/对比 |
| profiles | config、mods | undo |
| tags | mods | 标签详情 |
| history | updates | 自动更新入口 |
| backups | — | 恢复后 bump mods |
| saves | config | 存档解析 |
| config | config | 实例 CRUD、.rwmod 迁移 |
| settings | config | XML 编辑/备份 |

---

## 7. 迁移顺序与依赖

### 7.1 阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| F1（wave 1，FE-1） | `router/hashRouter` + `routes`、`layout/AppShell|Sidebar|Topbar|PanelHost`、`store/{ui,config,queue,updates,bus}`、`api/client|ws`、`CommandPalette`、`ToastViewport`、`DialogHost`、`ErrorBoundary`、手写 CSS token 化 | Vitest 组件测试；`navigation.spec`、`command-palette.spec` 在 React 入口上通过 |
| F2（wave 2，FE-2/FE-3） | 15 个面板按 §7.2 顺序重写；每完成一个，旧 panel 保留、新 panel 在 React 入口可用 | 旧入口 `test:e2e` 保持 53/1；React 入口逐条点亮 |
| F3（cutover，2026-11-07 ✅） | `index.html` 指向 `/src/app/main.tsx`；删除旧 `src/main.ts`、`src/panels/*.ts`、旧 `layout/api/ws/cmd/toast/router`、`style.css`、`react.html` | 全量 `test:e2e` **82 passed / 0 failed / 0 skipped**；`npm run build` 产物只有 `../static/index.html` |

> **过渡期双入口：已完成（2026-11-07 cutover）。** F1/F2 期间曾用 `frontend/react.html`
> + `FRONTEND_ENTRY=react` 并行跑两套入口；F3 已删除 `react.html` 与 `FRONTEND_ENTRY`
> 开关，React 成为 `/` 的唯一入口。

### 7.2 面板顺序（依赖优先、风险递增）

1. **只读、低风险**：`dashboard`（依赖 queue/config）→ `history` → `backups` → `saves` → `tags`。
2. **表单/配置**：`config`（实例 CRUD 最复杂，但无跨面板写）→ `settings`。
3. **本地写操作**：`mods` → `profiles` → `rimsort`。
4. **流式/共享状态**：`queue`（含更新区）→ `search` → `download` → `collection` → `import`。

理由：前三组不依赖 SSE/WS 的时序；`queue` 是下载/搜索/合集/导入的共同下游，必须先稳定
`store/queue` 与 WS 桥；`download/collection/import` 共用 `transfer` slice 的 SSE 模式，放在最后
统一实现一次。

---

## 8. 目录规划（FE-1 起）

```
frontend/src/
  app/            # 0b 已有 App.tsx / main.tsx；FE-1 加 bootstrap.tsx、commands.ts、shortcuts.ts
  api/            # 0b 已有 generated.ts；FE-1 加 client.ts、types.ts、ws.ts、sse.ts
  store/          # FE-1：ui.ts config.ts mods.ts queue.ts updates.ts transfer.ts bus.ts
  router/         # FE-1：routes.ts hashRouter.ts
  layout/         # FE-1：AppShell.tsx Sidebar.tsx Topbar.tsx PanelHost.tsx
  components/     # FE-1：CommandPalette.tsx ToastViewport.tsx DialogHost.tsx ErrorBoundary.tsx
                  #        EmptyState.tsx LoadingState.tsx ErrorState.tsx ConfirmDialog.tsx
  panels/         # FE-2/FE-3：XxxPanel.tsx（旧 src/panels/*.ts 已在 cutover 删除）
  styles/         # FE-1：tokens.css（从 style.css 抽 CSS 变量）、base.css、layout.css
frontend/index.html # 唯一 HTML 入口（cutover 后），加载 src/app/main.tsx
```

样式：继续手写 CSS，不做 CSS-in-JS、不引组件库；token 从现有 `style.css` 的 `--blue/--surface/--border`
等变量迁移，主题用 `<html data-theme="dark">`（旧实现是 `body.dark`，切换逻辑由 `ui` slice 统一）。

---

## 9. 风险与待决策

1. **E2E 兼容锚点**（最高风险）：React 组件若不保留 §6.1 的旧 id/class，53 条绿立刻回退。
   建议作为 FE-2/FE-3 任务书的强制验收项。
2. **`generated.ts` 大量 `unknown`**：后端路由缺 `response_model`。建议 BE-A/BE-B 在清 Major 时
   顺手补高频端点的 response_model；否则 FE-1 需要在 `api/types.ts` 维护一份手写窄化类型。
3. **route 选型**：自写 hash router 不引依赖；若用户希望直接上 react-router，需要显式授权新依赖。
4. **`react.html` 第二入口**：已随 F3 cutover 删除（2026-11-07）。
5. **原生 prompt**：已随旧入口删除，React 全部使用应用内弹窗。
6. **`index.html` 切换**：已完成（F3，2026-11-07）；PyInstaller 打包验证 `/` 返回 React 页面。
