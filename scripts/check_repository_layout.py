"""Check local Markdown links and literal repository references without running services.

Run from any directory. Historical prose/code paths are preserved as evidence;
Markdown links are checked in both current docs and archives.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>]+>|[^)\s]+)[^)]*\)")
REFERENCE = re.compile(r"`((?:tests|src|scripts|docs)/[^`\s]+\.(?:py|md|toml))`")


def check() -> list[str]:
    errors: list[str] = []
    pages = [ROOT / "README.md", ROOT / "ARCHITECTURE.md", ROOT / "tests/README.md"]
    pages.extend(sorted((ROOT / "docs").rglob("*.md")))
    for page in pages:
        fenced = False
        for number, line in enumerate(page.read_text(encoding="utf-8-sig").splitlines(), 1):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                continue
            for match in LINK.finditer(line):
                raw = unquote(match[1].strip("<>"))
                if raw.startswith(("#", "http:", "https:", "mailto:", "plugin:", "app:", "codex:")):
                    continue
                target = raw.split("#", 1)[0]
                if not target:
                    continue
                resolved = Path(target) if re.match(r"^[A-Za-z]:", target) else page.parent / target
                if not resolved.exists():
                    errors.append(f"{page.relative_to(ROOT).as_posix()}:{number}: missing link {raw}")
            if "archive" not in page.relative_to(ROOT).parts:
                for match in REFERENCE.finditer(line):
                    if not (ROOT / match[1]).exists():
                        errors.append(f"{page.relative_to(ROOT).as_posix()}:{number}: missing reference {match[1]}")
    return errors


if __name__ == "__main__":
    problems = check()
    for problem in problems:
        print(problem)
    print(f"Repository document layout: {len(problems)} issue(s)")
    raise SystemExit(bool(problems))
