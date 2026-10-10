from __future__ import annotations

import asyncio
import ctypes
import os
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path


# ctypes 声明严格设置 HANDLE 的返回类型，避免 64 位句柄被默认的 int 截断。
if os.name == "nt":
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    kernel = None


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


def _configure_api() -> None:
    if kernel is None:
        return
    definitions = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "CreatePipe": ([ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD], wintypes.BOOL),
        "SetHandleInformation": ([wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
        "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)], wintypes.BOOL),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "InitializeProcThreadAttributeList": ([ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        "UpdateProcThreadAttribute": ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
        "CancelIoEx": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
    }
    for name, (args, result) in definitions.items():
        function = getattr(kernel, name)
        function.argtypes = args
        function.restype = result
    if hasattr(kernel, "CreatePseudoConsole"):
        kernel.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        kernel.CreatePseudoConsole.restype = ctypes.c_long
        kernel.ResizePseudoConsole.argtypes = [wintypes.HANDLE, COORD]
        kernel.ResizePseudoConsole.restype = ctypes.c_long
        kernel.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
        kernel.ClosePseudoConsole.restype = None


_configure_api()


def _check(ok: object) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _close(handle: int | None) -> None:
    if handle:
        kernel.CloseHandle(handle)


def _pipe(*, inherit: bool) -> tuple[int, int]:
    attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, inherit)
    read, write = wintypes.HANDLE(), wintypes.HANDLE()
    _check(kernel.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(attributes), 0))
    return read.value, write.value


class WindowsBackend:
    """先挂起创建、加入 Job、再恢复执行，关闭托管句柄即回收整棵进程树。"""

    def __init__(self, *, pid: int, process: int, job: int, stdin: int,
                 outputs: dict[str, int], pseudoconsole: int | None) -> None:
        self.pid = pid
        self._process = process
        self._job = job
        self._stdin = stdin
        self._outputs = outputs
        self._pseudoconsole = pseudoconsole
        self.streams = tuple(outputs)
        self._closed = False
        self._code: int | None = None
        # 长期阻塞 ReadFile/Wait 不能占满 asyncio 默认线程池，阻止新任务创建。
        self._io_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"process-{pid}")
        self._pseudo_close_task: asyncio.Task | None = None

    async def _io(self, operation):
        return await asyncio.get_running_loop().run_in_executor(self._io_executor, operation)

    @classmethod
    def spawn(cls, command: str, cwd: Path, *, transport: str,
              columns: int, rows: int) -> WindowsBackend:
        if kernel is None:
            raise RuntimeError("Windows 后端只能在 Windows 使用。")
        owned: set[int] = set()
        attributes_initialized = False
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        process_info = PROCESS_INFORMATION()
        pseudoconsole = wintypes.HANDLE()
        try:
            job = kernel.CreateJobObjectW(None, None)
            _check(job)
            owned.add(job)
            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            _check(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            if transport == "pty":
                if not hasattr(kernel, "CreatePseudoConsole"):
                    raise RuntimeError("系统不支持 ConPTY，需要 Windows 10 1809 或更新版本。")
                child_in, parent_in = _pipe(inherit=False)
                parent_out, child_out = _pipe(inherit=False)
                owned.update((child_in, parent_in, parent_out, child_out))
                result = kernel.CreatePseudoConsole(COORD(columns, rows), child_in, child_out, 0, ctypes.byref(pseudoconsole))
                if result < 0:
                    raise OSError(f"CreatePseudoConsole HRESULT=0x{result & 0xffffffff:08x}")
                outputs = {"terminal": parent_out}
            else:
                child_in, parent_in = _pipe(inherit=True)
                parent_out, child_out = _pipe(inherit=True)
                parent_err, child_err = _pipe(inherit=True)
                owned.update((child_in, parent_in, parent_out, child_out, parent_err, child_err))
                for handle in (parent_in, parent_out, parent_err):
                    _check(kernel.SetHandleInformation(handle, 1, 0))
                startup.StartupInfo.dwFlags = 0x00000100 | 0x00000001  # STD_HANDLES | SHOWWINDOW
                startup.StartupInfo.hStdInput = child_in
                startup.StartupInfo.hStdOutput = child_out
                startup.StartupInfo.hStdError = child_err
                outputs = {"stdout": parent_out, "stderr": parent_err}
            size = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            _check(kernel.InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, ctypes.byref(size)))
            attributes_initialized = True
            if transport == "pty":
                _check(kernel.UpdateProcThreadAttribute(startup.lpAttributeList, 0, 0x00020016,
                                                       pseudoconsole, ctypes.sizeof(wintypes.HANDLE), None, None))
            else:
                handles = (wintypes.HANDLE * 3)(child_in, child_out, child_err)
                _check(kernel.UpdateProcThreadAttribute(startup.lpAttributeList, 0, 0x00020002,
                                                       handles, ctypes.sizeof(handles), None, None))
            shell = str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            import subprocess
            payload = ("$ProgressPreference='SilentlyContinue'; $OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                       + command + "\n$lancherCommandSucceeded=$?; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; "
                       "if (-not $lancherCommandSucceeded) { exit 1 }")
            arguments = [shell, "-NoLogo", "-NoProfile"]
            if transport == "pipe":
                arguments.append("-NonInteractive")
            # list2cmdline 只编码 argv，不对命令内容做二次 shell 解释。
            # -Command 保留普通文本 stderr，避免 -EncodedCommand 的 CLIXML 协议泄漏到日志。
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments + ["-Command", payload]))
            environment = dict(os.environ)
            environment.setdefault("PYTHONIOENCODING", "utf-8")
            environment_buffer = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].upper())) + "\0\0")
            flags = 0x00000004 | 0x00080000 | 0x00000400  # SUSPENDED | EXTENDED_STARTUPINFO | UNICODE_ENVIRONMENT
            if transport == "pipe":
                flags |= 0x08000000 | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP
            else:
                # 明确给出空标准句柄，让 ConPTY 为客户端初始化控制台句柄；
                # 否则宿主被重定向的 stdout 可能被外部子程序沿用，输出绕过终端。
                startup.StartupInfo.dwFlags = 0x00000100
            _check(kernel.CreateProcessW(shell, command_line, None, None, transport == "pipe", flags,
                                         environment_buffer, str(cwd), ctypes.byref(startup), ctypes.byref(process_info)))
            owned.update((process_info.hProcess, process_info.hThread))
            _check(kernel.AssignProcessToJobObject(job, process_info.hProcess))
            if kernel.ResumeThread(process_info.hThread) == 0xffffffff:
                raise ctypes.WinError(ctypes.get_last_error())
            for handle in (child_in, child_out, process_info.hThread):
                _close(handle)
                owned.discard(handle)
            if transport == "pipe":
                _close(child_err)
                owned.discard(child_err)
            backend = cls(pid=process_info.dwProcessId, process=process_info.hProcess, job=job,
                          stdin=parent_in, outputs=outputs, pseudoconsole=pseudoconsole.value)
            owned.clear()
            return backend
        except BaseException:
            if process_info.hProcess:
                kernel.TerminateProcess(process_info.hProcess, 1)
            if pseudoconsole:
                kernel.ClosePseudoConsole(pseudoconsole)
            raise
        finally:
            if attributes_initialized:
                kernel.DeleteProcThreadAttributeList(startup.lpAttributeList)
            for handle in owned:
                _close(handle)

    async def read(self, stream: str, size: int = 4096) -> bytes:
        handle = self._outputs[stream]
        def read() -> bytes:
            buffer = ctypes.create_string_buffer(size)
            length = wintypes.DWORD()
            if not kernel.ReadFile(handle, buffer, size, ctypes.byref(length), None):
                if ctypes.get_last_error() in {6, 109, 232, 995}:
                    return b""
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer.raw[:length.value]
        return await self._io(read)

    async def write(self, data: bytes) -> None:
        def write() -> None:
            offset = 0
            while offset < len(data):
                length = wintypes.DWORD()
                _check(kernel.WriteFile(self._stdin, data[offset:], len(data) - offset, ctypes.byref(length), None))
                if not length.value:
                    raise BrokenPipeError("进程输入管道已关闭。")
                offset += length.value
        await self._io(write)

    async def wait(self) -> int:
        if self._code is not None:
            return self._code
        def wait() -> int:
            result = kernel.WaitForSingleObject(self._process, 0xffffffff)
            if result == 0xffffffff:
                raise ctypes.WinError(ctypes.get_last_error())
            code = wintypes.DWORD()
            _check(kernel.GetExitCodeProcess(self._process, ctypes.byref(code)))
            return code.value if code.value < 0x80000000 else code.value - 0x100000000
        self._code = await self._io(wait)
        return self._code

    async def interrupt(self) -> None:
        if self._pseudoconsole:
            await self.write(b"\x03")
        # CREATE_NO_WINDOW 的管道进程没有控制台，不能声称发送了 CTRL_C。
        # Supervisor 的宽限期结束后仍执行 Job 级终止。

    async def terminate(self) -> None:
        if self._job:
            if not kernel.TerminateJobObject(self._job, 1):
                # Job 显式终止失败时仍可通过最后一个 KILL_ON_JOB_CLOSE 句柄收尾。
                _check(kernel.CloseHandle(self._job))
                self._job = None

    async def resize(self, columns: int, rows: int) -> None:
        if not self._pseudoconsole:
            raise ValueError("普通管道进程没有终端尺寸。")
        if not 1 <= columns <= 1000 or not 1 <= rows <= 1000:
            raise ValueError("终端尺寸超出范围。")
        result = kernel.ResizePseudoConsole(self._pseudoconsole, COORD(columns, rows))
        if result < 0:
            raise OSError("调整 ConPTY 尺寸失败。")

    async def finish_group(self) -> None:
        # 先停止同组后代，再让日志读取者继续排空管道。
        await self.terminate()
        if self._pseudoconsole:
            handle, self._pseudoconsole = self._pseudoconsole, None
            # ConPTY 关闭可能等待输出排空，不能阻塞 asyncio 主线程。
            self._pseudo_close_task = asyncio.create_task(self._io(lambda: kernel.ClosePseudoConsole(handle)))
            try:
                await asyncio.wait_for(asyncio.shield(self._pseudo_close_task), 3.0)
            except TimeoutError:
                # 收尾帧若不能排空，关闭输入输出通道解除 ConPTY 的同步等待。
                for channel in (self._stdin, *self._outputs.values()):
                    kernel.CancelIoEx(channel, None)
                    _close(channel)
                self._stdin = None
                self._outputs = {}
                await asyncio.wait_for(asyncio.shield(self._pseudo_close_task), 3.0)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.finish_group()
        finally:
            for handle in (self._stdin, *self._outputs.values()):
                kernel.CancelIoEx(handle, None)
                _close(handle)
            # 即使终止/ConPTY 报错，KILL_ON_JOB_CLOSE 仍是最后的进程树清理保障。
            _close(self._job)
            _close(self._process)
            self._job = None
            self._stdin = None
            self._outputs = {}
            self._io_executor.shutdown(wait=False, cancel_futures=True)
