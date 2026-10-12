from pathlib import Path
from importlib.util import find_spec


project_root = Path.cwd().resolve()
icon_path = project_root / "assets" / "lancher_code.ico"

# 终端发行包始终包含完整界面；普通的函数内导入仍由 PyInstaller 自动发现。
missing_ui_dependencies = [name for name in ("rich", "textual") if find_spec(name) is None]
if missing_ui_dependencies:
    raise RuntimeError(
        "打包终端界面缺少依赖："
        + ", ".join(missing_ui_dependencies)
        + "。请先运行 uv sync --extra build。"
    )


a = Analysis(
    ["main.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lancher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=str(icon_path),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="lancher",
)
