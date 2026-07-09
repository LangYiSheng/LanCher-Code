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


class PathSandboxError(ValueError):
    def __init__(self, raw_path: str, resolved_path: Path, root: Path) -> None:
        self.raw_path = raw_path
        self.resolved_path = resolved_path
        self.root = root
        super().__init__(f"路径越界，禁止访问项目目录之外的路径: {resolved_path}")


def resolve_path(cwd: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def ensure_path_in_root(path: Path, root: Path, *, raw_path: str | None = None) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PathSandboxError(raw_path or str(path), resolved_path, resolved_root) from exc
    return resolved_path


def resolve_path_in_root(cwd: Path, raw_path: str, root: Path) -> Path:
    resolved = resolve_path(cwd, raw_path)
    return ensure_path_in_root(resolved, root, raw_path=raw_path)


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
