"""One-off lint fixes for workshop.py — idempotent, safe to re-run."""

from pathlib import Path

WORKSHOP = Path(__file__).resolve().parent.parent / "src" / "rwmod" / "workshop.py"


def main() -> None:
    content = WORKSHOP.read_text(encoding="utf-8")
    original = content

    # 1. Ensure module-level `import re` exists (used by _scrape_collection_page).
    if "import re" not in content:
        content = content.replace("import json", "import re\nimport json", 1)
        print("Added import re to workshop.py")

    # 2. Wrap the two known-overlong header lines (E501).
    long_user_key = (
        'log.info("Collection %s: found %d mods via user API key", collection_id, len(result))'
    )
    content = content.replace(
        long_user_key,
        "log.info(\n"
        '                    "Collection %s: found %d mods via user API key",\n'
        "                    collection_id, len(result),\n"
        "                )",
    )

    long_headers = (
        'headers={"User-Agent": "rwmod/1.0", "Content-Type": "application/x-www-form-urlencoded"},'
    )
    content = content.replace(
        long_headers,
        "headers={\n"
        '                    "User-Agent": "rwmod/1.0",\n'
        '                    "Content-Type": "application/x-www-form-urlencoded",\n'
        "                },",
    )

    if content != original:
        WORKSHOP.write_text(content, encoding="utf-8", newline="\n")
        print("workshop.py fixed")
    else:
        print("workshop.py already clean")


if __name__ == "__main__":
    main()
