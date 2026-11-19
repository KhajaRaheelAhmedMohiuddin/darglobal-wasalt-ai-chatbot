"""Refreshes the copies of project files shown in the chat UI's "Docker & HF Space Deploy" tab.

The UI embeds these files in a `const CODE_FILES = {...};` line in app/static/index.html.
Run this after changing any of them:

    python scripts/sync_ui_code.py

tests/test_app.py fails if the embedded copies are out of date.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "app" / "static" / "index.html"
MARKER = "const CODE_FILES = "

# Key used by the UI -> file in the project
CODE_FILES = {
    "Dockerfile": "Dockerfile",
    "Dockerfile_scraper": "Dockerfile.scraper",
    "compose": "docker-compose.yml",
    "main": "app/main.py",
    "scraper": "app/scraper.py",
    "readme": "README.md",
}


def current_code_files() -> dict:
    return {key: (ROOT / path).read_text(encoding="utf-8") for key, path in CODE_FILES.items()}


def embedded_code_files(html: str) -> dict:
    start = html.index(MARKER) + len(MARKER)
    end = html.index("\n", start)
    return json.loads(html[start:end].rstrip().rstrip(";"))


def main() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index(MARKER)
    end = html.index("\n", start)
    # Escape "</" so file contents can never close the surrounding <script> tag
    payload = json.dumps(current_code_files(), ensure_ascii=False).replace("</", "<\\/")
    html = html[:start] + MARKER + payload + ";" + html[end:]
    INDEX.write_text(html, encoding="utf-8")
    print(f"Updated {len(CODE_FILES)} embedded files in {INDEX.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
