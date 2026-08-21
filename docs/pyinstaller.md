# PyInstaller 打包说明

LanCher Code 现在已经接好了 PyInstaller 的基础构建配置，入口文件是仓库根目录的 `main.py`，spec 文件是 `lancher.spec`。
Windows 可执行文件图标使用仓库里的 `assets/lancher_code.ico`。

## 1. 安装打包依赖

推荐继续使用 `uv`：

```bash
uv sync --extra build
```

这会把 `pyproject.toml` 里的 `build` 依赖组一起装进当前虚拟环境。

## 2. 构建 Windows 可执行文件

先使用更稳的 `onedir` 模式：

```bash
uv run pyinstaller --clean --noconfirm lancher.spec
```

构建完成后，主程序在：

```text
dist/lancher/lancher.exe
```

这个目录里的文件需要整体分发给用户，用户运行 `lancher.exe` 即可。

## 3. 如果你想要单文件 exe

`onefile` 分发更方便，但启动会稍慢，排查资源问题也更麻烦。建议先把 `onedir` 跑通，再切到 `onefile`。

可以直接用命令行试：

```bash
uv run pyinstaller --clean --noconfirm --onefile --icon assets/lancher_code.ico --name lancher main.py
```

构建完成后，文件通常在：

```text
dist/lancher.exe
```

## 4. 为什么这里使用 console 模式

LanCher Code 是终端/TUI 程序，不是桌面 GUI，所以需要保留控制台窗口。spec 中已经显式使用了：

```python
console=True
```

如果改成 `windowed=True` 或 `console=False`，终端交互会不正常。

## 5. 配置文件位置

程序当前读取的全局配置目录是：

```text
~/.lancher/lancher.yaml
```

这套路径逻辑基于 `Path.home()`，不是基于源码目录，所以在 PyInstaller 打包后仍然成立，通常不需要额外处理 `sys._MEIPASS`。

## 6. 什么时候需要改 spec

当前项目暂时没有额外的静态资源目录，也没有显式的 `datas` 和 `hiddenimports`。如果后面出现下面这些情况，就需要补 spec：

- 某些模块运行正常，但打包后报 `ModuleNotFoundError`
- 你开始引入模板、图标、内置配置、样式文件等外部资源
- 某些库通过动态导入加载插件，PyInstaller 自动分析不到

常见补法是：

```python
hiddenimports=["some.dynamic.module"]
datas=[("path/to/source", "target_dir")]
```

## 7. 推荐的开发流程

开发阶段：

```bash
uv run lancher
```

准备给别人分发时：

```bash
uv sync --extra build
uv run pyinstaller --clean --noconfirm lancher.spec
```

如果后面想把它接进 CI，再继续补一个专门的构建脚本或 GitHub Actions 就行。
