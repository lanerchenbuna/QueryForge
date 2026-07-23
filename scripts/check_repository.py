"""Check GitHub-facing repository hygiene without modifying project data."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_GITHUB_FILE_BYTES = 95 * 1024 * 1024
SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".queryforge",
    ".ruff_cache",
    ".venv",
    ".venv312",
    ".vinext",
    ".wrangler",
    ".next",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
REQUIRED_FILES = (
    ".env.example",
    ".gitattributes",
    ".github/workflows/quality.yml",
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "docs/README.md",
    "pyproject.toml",
    "sample_data/anime_streaming/anime_streaming.sqlite",
    "sample_data/anime_streaming/semantic_model.yml",
    "web/.openai/hosting.json",
    "web/app/page.tsx",
    "web/package.json",
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def repository_files() -> list[Path]:
    return sorted(
        path
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file()
        and not any(
            part in SKIP_PARTS or part.endswith(".egg-info") for part in path.parts
        )
        and path.name != ".env"
        and not (
            path.name.startswith(".env.") and path.name != ".env.example"
        )
    )


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    files = repository_files()
    for relative in REQUIRED_FILES:
        if not (PROJECT_ROOT / relative).is_file():
            errors.append(f"missing required repository file: {relative}")

    for path in files:
        relative = path.relative_to(PROJECT_ROOT)
        size = path.stat().st_size
        if size > MAX_GITHUB_FILE_BYTES:
            errors.append(
                f"{relative} is {size / 1024 / 1024:.1f} MiB; GitHub rejects "
                "files near or above 100 MiB"
            )
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"text file is not valid UTF-8: {relative}")
            continue
        if str(PROJECT_ROOT) in text:
            errors.append(f"machine-specific absolute workspace path in {relative}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"possible committed secret in {relative}")
        if path.suffix.lower() == ".md":
            errors.extend(_broken_markdown_links(path, text))

    database = PROJECT_ROOT / "sample_data/anime_streaming/anime_streaming.sqlite"
    if database.is_file():
        try:
            with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            if not integrity or integrity[0] != "ok":
                errors.append("bundled anime SQLite database failed integrity_check")
            if int(tables) < 15:
                errors.append("bundled anime SQLite database has fewer than 15 tables")
        except sqlite3.Error as exc:
            errors.append(f"bundled anime SQLite database is unreadable: {exc}")

    csv_exports = list(
        (PROJECT_ROOT / "sample_data/anime_streaming/tables").glob("*.csv")
    )
    if len(csv_exports) != 15:
        errors.append(
            f"expected 15 preserved anime CSV exports, found {len(csv_exports)}"
        )
    if not (PROJECT_ROOT / "LICENSE").is_file():
        warnings.append(
            "No LICENSE selected; choose one before making the repository public."
        )

    payload = {
        "status": "failed" if errors else "passed",
        "files_checked": len(files),
        "errors": errors,
        "warnings": warnings,
        "sample_data": {
            "sqlite_preserved": database.is_file(),
            "csv_exports_preserved": len(csv_exports),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if errors else 0


def _broken_markdown_links(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    for match in MARKDOWN_LINK.finditer(text):
        target = match.group(1).strip().strip("<>")
        if not target or target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        try:
            resolved.relative_to(PROJECT_ROOT)
        except ValueError:
            errors.append(
                f"{path.relative_to(PROJECT_ROOT)} links outside repository: {target}"
            )
            continue
        if not resolved.exists():
            errors.append(
                f"{path.relative_to(PROJECT_ROOT)} has broken link: {target}"
            )
    return errors


if __name__ == "__main__":
    raise SystemExit(main())
