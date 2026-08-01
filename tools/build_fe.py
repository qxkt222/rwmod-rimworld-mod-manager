"""Minimal frontend build — runs vite via subprocess."""

import shutil
import subprocess
import sys
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def main() -> int:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        print("npx not found in PATH", file=sys.stderr)
        return 1

    print("Building frontend...")
    result = subprocess.run(
        [npx, "vite", "build", "--outDir", "../static", "--emptyOutDir"],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.stdout:
        print(result.stdout[-1000:])
    if result.returncode != 0:
        print("FAILED:", result.stderr[-500:], file=sys.stderr)
        return 1
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
