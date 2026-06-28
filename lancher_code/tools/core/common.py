from __future__ import annotations

from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
}

UI_PATH_LIMIT = 200
MODEL_PATH_LIMIT = 800
MODEL_MATCH_LIMIT = 400
MODEL_TEXT_CHAR_LIMIT = 24000


def resolve_path(cwd: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def is_skipped_path(path: Path, root: Path) -> bool:
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def iter_files(root: Path, *, include: str | None = None) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        if include and not root.match(include):
            return []
        return [root.resolve()]

    files: list[Path] = []
    for path in root.rglob("*"):
        if is_skipped_path(path, root):
            continue
        if not path.is_file():
            continue
        if include and not path.match(include):
            continue
        files.append(path.resolve())
    return files


def relative_display_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())
