"""
rwmod 完整测试套件 — 一键运行

用法：
    .venv\\Scripts\\python.exe run_tests.py

自动安装缺失的 dev 工具后运行全部 6 项检查。
"""

import os
import subprocess
import sys
from pathlib import Path

# Windows consoles often default to GBK/ANSI; force UTF-8 so emoji/Chinese
# progress output doesn't crash with UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def run(cmd: list[str], label: str) -> bool:
    print(f"\n{'─' * 60}")
    print(f"📋 {label}")
    print(f"{'─' * 60}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [str(PYTHON)] + cmd,
        cwd=str(ROOT),
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return result.returncode == 0


def ensure_tool(module: str) -> bool:
    """Check if a module is importable. If not, pip install it."""
    check = subprocess.run(
        [str(PYTHON), "-c", f"import {module}"],
        capture_output=True,
    )
    if check.returncode == 0:
        return True
    print(f"  📦 安装 {module}...")
    install = subprocess.run(
        [str(PYTHON), "-m", "pip", "install", module],
        capture_output=True,
    )
    return install.returncode == 0


def main() -> int:
    results: list[tuple[str, bool]] = []

    # ── 确保基础 dev 工具可用 ────────────────────────────────
    print("🔧 检查 dev 工具...")
    for tool, _pkg in [("ruff", "ruff"), ("mypy", "mypy"), ("bandit", "bandit")]:
        ok = ensure_tool(tool)
        status = "✅" if ok else "❌ 安装失败"
        print(f"  {status} {tool}")
    print()

    # ── 1. Import smoke test ──────────────────────────────────
    # 修复了 backup 的 assertion：Path.stem 不含 .zip 扩展名
    results.append(
        (
            "新模块导入检查",
            run(
                [
                    "-c",
                    r"""
import sys, traceback
fails = []
checks = []

def check(name, code):
    checks.append(name)
    try:
        exec(code, {})
    except Exception as e:
        fails.append(f"{name}: {e}\n{traceback.format_exc()[-200:]}")

check("errors",
    "from rwmod.errors import ConfigError; e=ConfigError('test'); assert e.detail=='test'")
# 版本漂移检查：pyproject.toml 是唯一真源。这个断言过去两次写死后过期
# （0.4.3、0.5.0 —— 见 CHANGELOG.md:165），所以改为跨文件比对而非硬编码。
# The version check must NOT import rwmod.server: that module calls
# init_logging() at import time and appends to the real ~/.rwmod.log, so every
# gate run wrote to the developer's log (REV-017). pyproject.toml is parsed
# directly with tomllib instead; the FastAPI app is asserted at the source
# level (it must keep referencing __version__ rather than a second literal).
check("version",
    "import pathlib,re,tomllib\n"
    "import rwmod\n"
    "from rwmod import __version__\n"
    "root=pathlib.Path(rwmod.__file__).resolve().parents[2]\n"
    "declared=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))"
    "['project']['version']\n"
    "assert __version__==declared, '__init__=%s pyproject=%s' % (__version__, declared)\n"
    "# The web app used to carry its own literal, so /api/openapi.json could\n"
    "# advertise a version the package never shipped. Checked without importing\n"
    "# the app (import side effects would write the real log).\n"
    "server_src=(root/'src'/'rwmod'/'server.py').read_text(encoding='utf-8')\n"
    "assert 'version=__version__' in server_src, "
    "'server.py no longer reuses __version__'\n"
    "# The installer is hand-edited; this is the only place that catches it\n"
    "# drifting from pyproject.toml before a release is built.\n"
    "iss=(root/'rwmod.iss').read_text(encoding='utf-8')\n"
    "iss_ver=re.search(r'^AppVersion=(.+)$', iss, re.M).group(1).strip()\n"
    "assert iss_ver==declared, 'rwmod.iss=%s pyproject=%s' % (iss_ver, declared)")


check("backup",
    "from rwmod.backup import _backup_metadata\n"
    "from pathlib import Path\n"
    "r = _backup_metadata(Path('/x/a__b__c.zip'))\n"
    "assert r == {'workshop_id':'a','folder_name':'b','timestamp':'c'}, r")
# Offline state must reflect real call outcomes rather than a synthetic ping, so
# assert the marking actually moves what /api/status reads.
check("offline",
    "from rwmod.offline import get_status,mark_offline,mark_online\n"
    "mark_offline(); assert get_status()['online'] is False, get_status()\n"
    "mark_online(); assert get_status()['online'] is True, get_status()")
check("database_queue",
    "from rwmod.database import queue_upsert,queue_load_pending,queue_clear_done")
check("schemas",
    "from rwmod.models.schemas import ConfigResponse,DownloadRequest,DownloadResultItem\n"
    "from rwmod.models.schemas import OkResponse,ErrorResponse")
# The 0.7.0 extraction split downloader.py into a `download` package and
# steamcmd.py into `steamcmd_support`. If either loses a module from the wheel
# (or a name is renamed), that only shows up at runtime as an ImportError on
# the first download — import every module here.
check("download_pipeline",
    "from rwmod.download import core, deps, install, locks, outcomes")
check("steamcmd_support",
    "from rwmod.steamcmd_support import logparse, procs, taxonomy")
check("0.7.0_modules",
    "from rwmod.mod_health_check import get_health\n"
    "from rwmod.mod_rules import normalize_package_id, split_anchor_order\n"
    "from rwmod.mod_settings import list_mod_settings\n"
    "from rwmod.mod_sizes import get_sizes\n"
    "from rwmod.modsconfig_edit import read_active_mods\n"
    "from rwmod.steam_db import SteamDB\n"
    "from rwmod.xmlutil import parse_about_field, parse_xml_root\n"
    "from rwmod.cache_db import invalidate_all\n"
    "from rwmod.deps import get_config")

if fails:
    for f in fails:
        print(f'  ❌ {f}')
    sys.exit(1)
print(f'  ✅ 全部 {len(checks)} 项导入通过')
""",
                ],
                "新模块导入 + 核心逻辑验证",
            ),
        )
    )

    # ── 2. ruff lint ────────────────────────────────────────
    if ensure_tool("ruff"):
        results.append(
            (
                "ruff lint",
                run(
                    ["-m", "ruff", "check", "src/", "--select", "E,F,W,I,UP,B,SIM"],
                    "ruff lint 检查",
                ),
            )
        )
    else:
        # A missing linter is a broken environment, not a passing check. The
        # old `True` here let `python run_tests.py` print 6/6 with no linting
        # at all when pip install failed offline.
        results.append(("ruff lint (安装失败)", False))

    # ── 3. ruff format ──────────────────────────────────────
    if ensure_tool("ruff"):
        results.append(
            (
                "ruff format",
                run(
                    ["-m", "ruff", "format", "--check", "src/"],
                    "ruff format 检查",
                ),
            )
        )
    else:
        results.append(("ruff format (安装失败)", False))

    # ── 4. pytest (no network) ──────────────────────────────
    results.append(
        (
            "pytest (无网络)",
            run(
                [
                    "-m",
                    "pytest",
                    "tests/",
                    "-m",
                    "not network",
                    "--cov=rwmod",
                    "--cov-report=term-missing",
                    # Same floor as .github/workflows/ci.yml. The local gate and
                    # CI must not disagree about what "too low" means.
                    "--cov-fail-under=90",
                    "-v",
                    "--tb=short",
                ],
                "单元测试 + 覆盖率",
            ),
        )
    )

    # ── 5. mypy ─────────────────────────────────────────────
    if ensure_tool("mypy"):
        results.append(
            (
                "mypy 类型检查",
                run(
                    ["-m", "mypy", "src/rwmod/", "--config-file=pyproject.toml"],
                    "mypy 类型检查",
                ),
            )
        )
    else:
        results.append(("mypy (安装失败)", False))

    # ── 6. bandit ───────────────────────────────────────────
    if ensure_tool("bandit"):
        results.append(
            (
                "bandit 安全扫描",
                run(
                    ["-m", "bandit", "-c", "pyproject.toml", "-r", "src/", "-ll"],
                    "bandit 安全扫描",
                ),
            )
        )
    else:
        results.append(("bandit (安装失败)", False))

    # ── summary ──────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print("📊 测试结果汇总")
    print(f"{'═' * 60}")
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n  {passed}/{passed + failed} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
