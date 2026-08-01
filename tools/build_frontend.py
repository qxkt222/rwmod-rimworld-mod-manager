"""Build rwmod frontend via Vite — used when shell npm is unavailable."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def main() -> int:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        print("npx not found in PATH — install Node.js first", file=sys.stderr)
        return 1

    print("[1/2] Building frontend...")
    result = subprocess.run(
        [npx, "vite", "build", "--outDir", "../static", "--emptyOutDir"],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print("STDERR:", result.stderr, file=sys.stderr)
        return 1
    print("✓ Build complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
