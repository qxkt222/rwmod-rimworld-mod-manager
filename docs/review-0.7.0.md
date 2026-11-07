# rwmod 0.7.0 全量代码审查报告

> 审查时间：2026-10-31
> 基线：HEAD `967daa5`（0.7.0），工作区仅多出本报告与构建产物
> 方式：6 路只读审查（下载内核 / API 与安全 / 数据层 / 排序与 Mod 管理 / 前端 / 打包与 CI），
> 关键结论由主审二次验证。
> 门禁状态：`run_tests.py` 6/6，pytest 1382 passed；安装包已构建并冒烟通过。

---

## 结论

**当前 0.7.0 不建议直接对外发布。** 共 4 个 blocker，其中 2 个会直接损坏用户数据或系统，1 个让安装版默认不可用。

| # | 级别 | 位置 | 问题 |
|---|---|---|---|
| B1 | blocker | `src/rwmod/routers/mods.py:396-407` | 驱动器相对路径绕过删除守卫，可 `rmtree` 任意目录 |
| B2 | blocker | `src/rwmod/steamcmd.py:529-554` | 显式下载失败被旧内容覆盖为成功 |
| B3 | blocker | `rwmod.iss:6,13,19-20` | 装到 Program Files 但以普通权限运行，SteamCMD 无法写入 |
| B4 | blocker | `Dockerfile:31-32,51-60` + `docker-compose.yml:13,17` | 容器非 root 用户无家目录、`VOLUME` 后 chown 失效、steamcmd 只读挂载 → 启动即失败 |

---

## Blocker 详情（均已复核）

### B1 驱动器相对路径绕过删除守卫
`routers/mods.py` 的守卫只拒绝 `""` / `.` / `..` / `/` / `\`：

```python
if not folder or folder in (".", "..") or "/" in folder or "\\" in folder:
```

Windows 上 `cfg.mods_dir / "C:Windows"` 得到 `C:Windows`，`Path.resolve()` 为 `C:\Windows`，不含任何分隔符即可通过守卫；随后 `backup_mod` 因无 workshop_id 跳过备份，`shutil.rmtree` 直接删除。
实测：`Path('D:/RimWorld/Mods') / 'C:Windows'` → `C:Windows`；`Path('C:Windows').resolve()` → `C:\Windows`。

修法：拒绝含 `:`、`Path(folder).drive` 非空、`Path(folder).name != folder` 的输入；删除前断言
`target.resolve().is_relative_to(cfg.mods_dir.resolve())`。

### B2 失败下载被判定为成功
`steamcmd.py` 解析日志后：

```python
elif content_exists:
    # No usable log line, but the files are on disk ...
    results[mid] = DownloadResult(success=True, ...)
```

`content_exists` 只判断旧内容目录是否存在。SteamCMD 明确写了 `Failure` / `File Not Found` / `Access Denied` 时，只要此前下载过该 Mod，仍会返回 success，随后旧内容被重新安装并 `set_last_updated(now)`：更新静默未生效，且不会再被标记为过期。

修法：仅当日志中没有该 id 的结果行（UNKNOWN）时才信任文件系统；显式失败优先。

### B3 安装版默认不可用（权限）
`rwmod.iss`：`PrivilegesRequired=admin`，`DefaultDirName={commonpf}\rwmod`；快捷方式以 `asInvoker` 启动。
SteamCMD 以 `cwd={app}\steamcmd` 运行并写 `logs/`、`steamapps/`（`steamcmd.py:428`）。普通用户对 `Program Files` 无写权限 → 所有下载失败。
另外 `steamapps` 不在卸载清单，卸载后残留数 GB。

修法：`[Dirs] Name:"{app}\steamcmd"; Permissions: users-modify`，或改 `{localappdata}` + `PrivilegesRequired=lowest`；卸载时清理 `steamapps`。

### B4 Docker 启动失败
- `useradd -r` 不创建 `/home/rwmod`，镜像未 `mkdir`；`Path.home()/".rwmod.db"` 不可写 → `sqlite3.OperationalError`，uvicorn 启动失败。
- `VOLUME` 声明在 `mkdir/chown` 之前，按 Docker 规则其后的 chown 不进入镜像，卷为 root:root。
- compose 把 `./steamcmd` 以 `:ro` 挂载，SteamCMD 需要写 `steamapps/logs`。
- compose 把 `rwmod_config` 挂到 `/home/rwmod`，该路径在镜像中不存在。

修法：`ENV HOME=/app/data` + 构建期创建并 chown；compose 改挂 `/app/data`；steamcmd 改读写挂载。

---

## Major（按领域）

### API / 安全
| 位置 | 问题 | 修法 |
|---|---|---|
| `routers/config.py:115-127` + `routers/dashboard.py:141-150` | `steamcmd_path` 只校验“绝对路径且是文件”，check 接口直接执行；`import-local` 可上传任意 `.exe` 到 mods_dir → 上传→配置→执行链 | 要求 basename 为 `steamcmd.exe` 或固定目录，禁止可上传目录作为可执行路径 |
| `routers/transfer.py:79-95` | async 端点内同步解压/复制最多 4GB，阻塞事件循环 | `asyncio.to_thread` |
| `routers/saves.py:71-75`、`routers/rimsort.py:71-74` | 分块上传绕过 5MB 上限，`file.read()` 无界读入内存 | 统一 `read_upload_limited` |
| `routers/mods.py:296-325` | 2GB zip 整体读入内存并再复制一份，单请求峰值 4GB+ | 流式落盘 + `ZipFile(path)` |
| `routers/workshop.py:29-32,59-65` | Steam 故障被吞成 200 空结果/`found:false` | 抛 `WorkshopError`（502），仅“确实无结果”返回空 |
| `routers/download.py:469-487` | async 内同步做两次全量 Mod 扫描 | `to_thread` |
| `routers/backups.py`、`profiles.py`、`undo.py`、`auto_update.py`、`parser.py`、`transfer.py` | 多处失败返回 200 `{ok:false}`，违反失败契约 | 抛 `errors.py` 异常；`parse_mods_config` 语法错误抛 `ValidationError` |
| `routers/download.py:155-161` | 非 UTF-8 modlist 抛 `UnicodeDecodeError` → 500 | 复用多编码回退，失败 400 |
| `server.py:311-338` | WebSocket 不校验 Origin，任意网页可读队列数据 | 握手校验 Origin 或一次性 token |
| `parser.py:156-161` 等 | 不可信 XML 走 stdlib ElementTree，依赖 expat 版本 | 统一 `xmlutil.parse_xml_root`；导入 profile 时先 defused 校验 |
| `models/schemas.py:166-169` + server | JSON body 无大小上限，LAN 客户端可 OOM | body 上限中间件 + 字段 `max_length` |

### 数据层 / 实例隔离
| 位置 | 问题 | 修法 |
|---|---|---|
| `config.py:336`、`:493-502` | TOML 带 BOM/UTF-16 时 `tomllib` 抛错且无兜底 → 全站 500；`current_instance_id()` 静默回退 default，写入串实例 | `utf-8-sig` 读取 + 捕获 `TOMLDecodeError`；禁止静默回退 |
| `queue.py:325-327` + `database.py:699-701` | `clear_done` 用无实例 `_db_delete(wid)`，删掉其他实例同 id 的 pending 行 | 按 `(id, instance_id)` 删除 |
| `queue.py:239-259` | 删除/取消只按 mod_id；`_cancelled`、`_live_procs` 也按 mod_id → 跨实例误取消 | 以 `(instance_id, id)` 为键 |
| `parser.py:181-190` | `resolve_workshop_ids` 未做大小写归一 → 驼峰 packageId 误报 missing | 统一 `normalize_package_id` |
| `undo.py:106` | `shutil.copy2` 直接覆盖 live ModsConfig，中断留截断文件 | tmp + `os.replace` |
| `profile.py:290-295` | 先移走 live 文件再替换，第二步失败后 ModsConfig 消失 | 直接原子覆盖；失败恢复 `.bak` |
| `cache_db.py:54-56` | mods_dir 暂不可用时清空元数据表 → 更新永久漏报 | 目录缺失时返回空，不清库 |
| `routers/mods.py:76` | 标签查询未带 `instance_id`，跨实例泄漏 | 加 `WHERE instance_id = ?` |

### 前端
| 位置 | 问题 |
|---|---|
| `panels/updates.ts:18,26-29,36` | 无更新时提前 `return`，跳过 `btn.disabled=false` → 按钮永久禁用 |
| `main.ts:105-107` + `panels/dashboard.ts:83-123` | 离开 dashboard 停止轮询，返回后不再启动 → 一键更新按钮卡死 |
| `cmd.ts:94-108,112-121` | 键盘导航索引用全量 `COMMANDS`，与实际过滤列表不符 → 回车执行错命令 |
| `panels/download.ts:12,129-174` + `collection.ts` | 无在途保护，双流并发互踩进度/日志/controller |
| `panels/collection.ts:143-151` | `fail` 分支直接 return，失败原因不显示 |
| `panels/config.ts`、`profiles.ts`、`queue.ts` | 切换实例/恢复 profile/队列完成后不刷新 Mods 列表 |
| `panels/queue.ts` + `main.ts` | `setStatus` 只 `console.log`，`#statusbar` 从未创建 → 操作失败零反馈 |
| `panels/updates.ts:40-77` | 每条更新渲染两份完整描述，553 Mod 时 innerHTML 数十 MB |

### 下载内核
| 位置 | 问题 |
|---|---|
| `downloader.py:449-455,796-808` | stash 失败后裸调 `rmtree`，异常掀翻整批；无备份时旧版丢失 |
| `queue.py:242-268,600-618` + `steamcmd.py:396-399` | 等待全局锁/探测阶段取消无效，取消后仍会下载并安装 |
| `downloader.py:633-644` | 安装锁按 workshop id 而非目标文件夹，重复 packageId 并发安装互相删除 |
| `queue.py:763-772` | force 合集子项重新入队丢失 force → 已安装子项永远不更新 |
| `workshop.py:427-439` + `autoupdate.py` | Steam API 失败被当成“全部最新” |

### 排序 / Mod 管理
| 位置 | 问题 |
|---|---|
| `modsconfig_edit.py:288-311` | 并发读改写共用同一 tmp 名 → 丢更新/500 |
| `profile.py:280-295` | restore 不校验 well-formed，且非原子替换（同 B 类数据风险） |
| `routers/rimsort.py:170-246` | apply 无请求体、无确认、无鉴权，可被跨站简单请求触发 |
| `rimpy_db.py:359-364` | `refresh_rules=true` 在 24h 内是无声 no-op |
| `steam_db.py:227-236`、`rimpy_db.py:319-328` | `resp.read()` 无上限，60MB/2MB 上限不成立 |
| `rimsort.py:193-200` | compare 只豁免 Core，不豁免 DLC → 实测真实 522 Mod 配置误报 5 个缺失 |
| `routers/rimsort.py:229-238` | apply 用 stdlib ET 整篇重写，丢 BOM/注释/CRLF |
| `routers/config.py:174-176` + `profile.py:138` | `config_dir` 可指向任意可写目录，写入可超出游戏目录 |
| `mod_settings.py:67-69` | 备份目录不按实例分域，多实例同名文件串备份 |

### 打包 / CI
| 位置 | 问题 |
|---|---|
| `ci.yml:49-52` | `--cov-fail-under=55`，实际覆盖 92%，阈值形同虚设 |
| `.github/workflows/ci.yml` | 无 Windows 打包 job；spec/iss 改错 CI 全绿 |
| `.dockerignore` | 未排除 `steamcmd/`（7GB） |
| `rwmod.spec:138` | `steamcmd_siteserverui_win64.zip*` 在 Tree excludes 中永远匹配不到 |
| `rwmod.spec` + `rwmod.iss` | 打包含 `steamcmd/package`（约 30MB 自更新 zip），exe 82MB / setup 121MB |
| `src/rwmod/server.py:104` | 版本号两处硬编码，`run_tests.py` 断言不覆盖 iss/server |
| `run_tests.py:69-127` | “新模块导入检查”未导入 0.7.0 的任何新模块 |
| `run_tests.py:141,155,190,204` | 工具安装失败记为 `True`（跳过=通过） |
| `pyproject.toml:35` | `rwmod-web = "rwmod.server:app"` 把 ASGI 对象当脚本，执行即 TypeError |
| `.pre-commit-config.yaml` | ruff/mypy/bandit 版本与 uv.lock 不一致 |
| `Dockerfile:9-12` + `ci.yml` | 只有 `bun.lock` 却用 `npm install`，前端构建不可复现 |
| `Dockerfile:22-24` | pip 按 `>=` 现场解析，不走 uv.lock |

---

## 已验证无问题

- `utils.safe_extract_zip`：zip-slip、炸弹、重复路径防护完整。
- Skymods SSRF 防护（域名白名单 + DNS 私网判定 + 重定向复检）。
- SteamCMD id 纯数字过滤；SQL 全部参数化；`/static` 无穿越。
- 前端转义规则全量核对：无裸插值，`escAttr` 用于属性位，无 XSS 路径。
- 排序：真实 522 Mod 配置往返字节一致，0 unresolved / 0 cycles；官方锚定顺序正确。
- 测试：0.7.0 新模块均有测试引用；`hiddenimports` 缺项已被静态 import 覆盖，打包后实测可用。
- 失败重试、临时目录、Popen 回收无泄漏；未发现死锁路径。

---

## 建议发布顺序

1. 修 B1、B2、B3（影响发布物，改动小）。
2. B4 与 major 项排入 0.7.1（Docker / 前端体验 / 契约统一）。
3. 修完重跑 `run_tests.py` + 重新打包 + 冒烟，再发 v0.7.0。
