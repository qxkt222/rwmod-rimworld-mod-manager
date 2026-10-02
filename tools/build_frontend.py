"""Build rwmod frontend via Vite — used when shell npm is unavailable."""

import shutil
import subprocess
import sys
from pathlib import Path

# Windows consoles often default to GBK/ANSI; force UTF-8 exactly as run_tests.py
# does. Without this the script exits 1 on a cp936 console even when the build
# succeeded: reading vite's UTF-8 output raises UnicodeDecodeError, and printing
# the "✓" below raises UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def main() -> int:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        print("npx not found in PATH — install Node.js first", file=sys.stderr)
        return 1

    print("Building frontend...")
    result = subprocess.run(
        [npx, "vite", "build", "--outDir", "../static", "--emptyOutDir"],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(result.stdout)
    if result.returncode != 0:
        print("STDERR:", result.stderr, file=sys.stderr)
        return 1
    print("✓ Build complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
