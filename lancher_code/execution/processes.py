from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lancher_code.execution.backends import ProcessBackend, spawn_backend
from lancher_code.execution.contracts import ExecutionLimits, OutputPage, ProcessInfo, ProcessSpec
from lancher_code.execution.output import OutputLimitExceeded, OutputStore, read_saved_output
from lancher_code.sessions.paths import SessionPaths, validate_control_path, validate_session_id


TERMINAL_STATUSES = frozenset({"exited", "failed", "cancelled", "interrupted", "lost"})
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _uninterrupted(task: asyncio.Future):
    """停止已确定后，重复取消也不能打断取得句柄与释放资源。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@dataclass(slots=True)
class _Managed:
    info: ProcessInfo
    spec: ProcessSpec
    directory: Path
    output: OutputStore
    lease: object | None = None
    backend: ProcessBackend | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    monitor: asyncio.Task | None = None
    readers: list[asyncio.Task] = field(default_factory=list)
    stop_task: asyncio.Task | None = None
    auxiliary: list[asyncio.Task] = field(default_factory=list)
    stop_reason: str | None = None
    output_failed: bool = False
    event_error: Exception | None = None


class ProcessSupervisor:
    """托管进程的真实生命周期，与启动它的工具调用生命周期分开。"""

    def __init__(self, project_root: Path, *, limits: ExecutionLimits | None = None,
                 event_sink: Callable | None = None,
                 saved_processes: Callable[[str], list[dict]] | None = None) -> None:
        self.project_root = project_root.resolve()
        self.limits = limits or ExecutionLimits()
        self.event_sink = event_sink or (lambda *args, **kwargs: None)
        self.saved_processes = saved_processes
        self._processes: dict[str, _Managed] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._stopping_sessions: set[str] = set()
        self._stopping_turns: set[tuple[str, str | None]] = set()

    def _emit(self, entry: _Managed, event_type: str, *, strict: bool = False) -> None:
        entry.info.updated_at = _now()
        failure = None
        try:
            self.event_sink(entry.info.session_id, event_type, entry.info.to_dict(),
                            turn_id=entry.info.origin_turn_id)
        except Exception as exc:
            entry.event_error = exc
            entry.info.storage_error = str(exc)
            failure = exc
            logger.error("进程事件保存失败，资源清理仍继续：%s", event_type, exc_info=exc)
        try:
            self._save_metadata(entry)
        except Exception as exc:
            # meta 是查询缓存；事实日志已提交后，缓存故障不能撤销后台转交等状态。
            entry.event_error = exc
            entry.info.storage_error = str(exc)
            logger.error("进程查询缓存保存失败：%s", event_type, exc_info=exc)
            if event_type == "process.starting":
                # 初始查询记录也必须可保存，才能建立可恢复的进程身份。
                failure = failure or exc
        if failure is not None:
            if strict:
                raise failure
            if event_type not in {"process.exited", "process.stopping"} and not entry.done.is_set():
                asyncio.create_task(self.stop(entry.info.process_id, entry.info.session_id, reason="event_error"))

    def _save_metadata(self, entry: _Managed) -> None:
        path = entry.directory / "meta.json"
        temporary = entry.directory / "meta.tmp"
        for candidate in (path, temporary):
            validate_control_path(self.project_root, candidate)
        data = json.dumps(entry.info.to_dict(), ensure_ascii=False, indent=2)
        with temporary.open("w", encoding="utf-8") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)

    async def start(self, spec: ProcessSpec, *, session_id: str, turn_id: str | None,
                    invocation_id: str, resource_lease=None, cancellation_token=None) -> ProcessInfo:
        validate_session_id(session_id)
        if not spec.command.strip() or not spec.description.strip():
            raise ValueError("命令和描述不能为空。")
        if not 0 <= spec.yield_ms <= 60000:
            raise ValueError("yield_ms 必须在 0 至 60000 之间。")
        if spec.max_runtime_ms is not None and spec.max_runtime_ms <= 0:
            raise ValueError("运行期限必须大于零。")
        if spec.lifetime not in {"turn", "session"} or spec.transport not in {"pipe", "pty"}:
            raise ValueError("进程归属或传输类型无效。")
        cwd = spec.cwd.resolve()
        if not cwd.is_dir() or not cwd.is_relative_to(self.project_root):
            raise ValueError("命令工作目录必须位于项目内的现存目录。")
        if not 1 <= spec.columns <= 1000 or not 1 <= spec.rows <= 1000:
            raise ValueError("终端尺寸超出范围。")
        async with self._lock:
            if self._closed or session_id in self._stopping_sessions or (session_id, turn_id) in self._stopping_turns:
                raise RuntimeError("执行范围已停止，不能启动新进程。")
            if cancellation_token is not None and cancellation_token.is_cancelled:
                raise asyncio.CancelledError
            active = [entry for entry in self._processes.values() if not entry.done.is_set()]
            if len(active) >= self.limits.max_processes:
                raise RuntimeError("应用的活动进程数量达到上限。")
            if sum(entry.info.session_id == session_id for entry in active) >= self.limits.max_processes_per_session:
                raise RuntimeError("Session 的活动进程数量达到上限。")
            process_id = uuid.uuid4().hex
            directory = SessionPaths.for_session(self.project_root, session_id).root / "processes" / process_id
            output = OutputStore(directory, project_root=self.project_root,
                                 max_bytes=self.limits.output_limit_bytes)
            info = ProcessInfo(process_id, session_id, turn_id, invocation_id, spec.command,
                               spec.description, str(cwd), spec.transport, spec.lifetime,
                               readiness="pending" if spec.readiness else "unknown")
            entry = _Managed(info, replace(spec, cwd=cwd), directory, output)
            # 起始事件落盘成功后才有权执行命令。失败不会产生真实进程。
            self._emit(entry, "process.starting", strict=True)
            self._processes[process_id] = entry
            if resource_lease is not None:
                resource_lease.transfer()
                entry.lease = resource_lease
                bind_process = getattr(resource_lease, "bind_process", None)
                if callable(bind_process):
                    # 等待者需要知道现在由哪个真实进程占用资源，不能继续只显示启动调用。
                    bind_process(process_id)
        spawn_task = asyncio.create_task(spawn_backend(spec.command, cwd, transport=spec.transport,
                                                       columns=spec.columns, rows=spec.rows))
        token_task = asyncio.create_task(cancellation_token.wait()) if cancellation_token is not None else None
        try:
            # 即使在创建窗口取消，也先拿到系统句柄，再完整回收。
            if token_task is not None:
                done, _ = await asyncio.wait({spawn_task, token_task}, return_when=asyncio.FIRST_COMPLETED)
                if token_task in done:
                    raise asyncio.CancelledError
            entry.backend = await asyncio.shield(spawn_task)
            entry.info.pid = entry.backend.pid
            entry.info.status = "stopping" if entry.stop_reason is not None else "running"
            entry.info.started_at = _now()
            entry.readers = [asyncio.create_task(self._read_stream(entry, stream)) for stream in entry.backend.streams]
            entry.monitor = asyncio.create_task(self._monitor(entry))
            self._emit(entry, "process.started", strict=True)
            if entry.stop_reason is not None:
                await self.stop(process_id, session_id, reason=entry.stop_reason)
                return replace(entry.info)
            if spec.max_runtime_ms is not None:
                entry.auxiliary.append(asyncio.create_task(self._deadline(entry)))
            if spec.readiness:
                entry.auxiliary.append(asyncio.create_task(self._readiness(entry)))
            if spec.yield_ms:
                wait_task = asyncio.create_task(entry.done.wait())
                try:
                    waits = {wait_task}
                    if token_task is not None:
                        waits.add(token_task)
                    done, _ = await asyncio.wait(waits, timeout=spec.yield_ms / 1000,
                                                return_when=asyncio.FIRST_COMPLETED)
                    if token_task is not None and token_task in done:
                        raise asyncio.CancelledError
                finally:
                    wait_task.cancel()
                    await asyncio.gather(wait_task, return_exceptions=True)
            return replace(entry.info)
        except asyncio.CancelledError:
            try:
                if entry.backend is None:
                    entry.backend = await _uninterrupted(spawn_task)
                    entry.info.pid = entry.backend.pid
                    entry.readers = [asyncio.create_task(self._read_stream(entry, stream)) for stream in entry.backend.streams]
                    entry.monitor = asyncio.create_task(self._monitor(entry))
                await _uninterrupted(asyncio.create_task(self.stop(process_id, session_id)))
            except Exception:
                await self._failed_start(entry, "spawn_failed")
            raise
        except BaseException:
            if entry.backend is not None:
                await _uninterrupted(asyncio.create_task(self.stop(process_id, session_id, reason="start_failed")))
            else:
                await self._failed_start(entry, "spawn_failed")
            raise
        finally:
            if token_task is not None:
                token_task.cancel()
                await asyncio.gather(token_task, return_exceptions=True)

    async def _failed_start(self, entry: _Managed, reason: str) -> None:
        entry.info.status = "failed"
        entry.info.exit_reason = reason
        self._emit(entry, "process.exited")
        try:
            if entry.lease is not None:
                await entry.lease.release()
        finally:
            entry.done.set()

    async def _read_stream(self, entry: _Managed, stream: str) -> None:
        try:
            while chunk := await entry.backend.read(stream):
                if entry.output_failed:
                    continue
                try:
                    await entry.output.append(stream, chunk)
                    entry.info.output_chars = entry.output.cursor
                    entry.info.output_bytes = entry.output.size_bytes
                except OutputLimitExceeded:
                    entry.output_failed = True
                    asyncio.create_task(self.stop(entry.info.process_id, entry.info.session_id, reason="output_limit"))
                except Exception:
                    entry.output_failed = True
                    asyncio.create_task(self.stop(entry.info.process_id, entry.info.session_id, reason="output_error"))
            if not entry.output_failed:
                await entry.output.append(stream, b"", final=True)
                entry.info.output_chars = entry.output.cursor
                entry.info.output_bytes = entry.output.size_bytes
        except asyncio.CancelledError:
            raise
        except Exception:
            entry.output_failed = True
            asyncio.create_task(self.stop(entry.info.process_id, entry.info.session_id, reason="output_error"))

    async def _monitor(self, entry: _Managed) -> None:
        try:
            entry.info.exit_code = await entry.backend.wait()
            # 根进程退出也要回收继承管道的后代，否则 EOF 和资源锁永远不到。
            finish = getattr(entry.backend, "finish_group", entry.backend.terminate)
            await finish()
            try:
                await asyncio.wait_for(asyncio.gather(*entry.readers, return_exceptions=True),
                                       self.limits.drain_timeout_seconds)
            except TimeoutError:
                entry.info.exit_reason = entry.stop_reason or "drain_timeout"
            entry.info.status = "cancelled" if entry.stop_reason in {"cancelled", "input_cancelled"} else (
                "failed" if entry.stop_reason in {"output_limit", "output_error", "runtime_limit", "start_failed", "input_timeout", "event_error"}
                else "exited")
            entry.info.exit_reason = entry.info.exit_reason or entry.stop_reason or "completed"
        except Exception as exc:
            entry.info.status = "interrupted"
            entry.info.exit_reason = f"backend_error: {exc}"
            try:
                await entry.backend.terminate()
            except Exception:
                pass
        finally:
            for task in entry.auxiliary:
                task.cancel()
            await asyncio.gather(*entry.auxiliary, return_exceptions=True)
            try:
                await entry.backend.close()
            except Exception as exc:
                entry.info.status = "interrupted"
                entry.info.exit_reason = f"cleanup_error: {exc}"
                logger.error("进程后端收尾异常：%s", entry.info.process_id, exc_info=exc)
            try:
                if entry.lease is not None:
                    await entry.lease.release()
            except Exception as exc:
                entry.info.status = "interrupted"
                entry.info.exit_reason = f"lease_error: {exc}"
                logger.error("进程资源锁释放异常：%s", entry.info.process_id, exc_info=exc)
            finally:
                self._emit(entry, "process.exited")
                entry.done.set()

    async def _deadline(self, entry: _Managed) -> None:
        await asyncio.sleep(entry.spec.max_runtime_ms / 1000)
        # 计时器不等待自己的 monitor；收尾时 monitor 会取消辅助任务。
        asyncio.create_task(self.stop(entry.info.process_id, entry.info.session_id, reason="runtime_limit"))

    async def _readiness(self, entry: _Managed) -> None:
        probe = entry.spec.readiness
        deadline = asyncio.get_running_loop().time() + probe.timeout_ms / 1000
        while not entry.done.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(probe.host, probe.port), 0.5)
                writer.close()
                await writer.wait_closed()
                entry.info.readiness = "ready"
                try:
                    self._emit(entry, "process.ready", strict=True)
                except Exception:
                    asyncio.create_task(self.stop(entry.info.process_id, entry.info.session_id, reason="event_error"))
                return
            except (OSError, TimeoutError):
                await asyncio.sleep(0.1)
        if not entry.done.is_set():
            entry.info.readiness = "timeout"
            self._emit(entry, "process.readiness_timeout")

    def _entry(self, process_id: str, session_id: str) -> _Managed:
        validate_session_id(session_id)
        validate_session_id(process_id)
        entry = self._processes.get(process_id)
        if entry is None or entry.info.session_id != session_id:
            raise ValueError("找不到属于当前 Session 的进程。")
        return entry

    def _authoritative_processes(self, session_id: str) -> dict[str, dict]:
        validate_session_id(session_id)
        if self.saved_processes is None:
            return {}
        result = {}
        for record in self.saved_processes(session_id):
            if not isinstance(record, dict):
                raise ValueError("Session 进程投影格式无效。")
            process_id = validate_session_id(record.get("process_id"))
            if record.get("session_id") != session_id:
                raise ValueError("Session 进程投影归属不一致。")
            result[process_id] = record
        return result

    def _saved_info(self, process_id: str, session_id: str, *, records: dict[str, dict] | None = None) -> ProcessInfo:
        validate_session_id(process_id)
        validate_session_id(session_id)
        authoritative = self._authoritative_processes(session_id) if records is None else records
        record = authoritative.get(process_id)
        if record is None:
            root = SessionPaths.for_session(self.project_root, session_id).root
            path = root / "processes" / process_id / "meta.json"
            validate_control_path(self.project_root, path)
            if not path.is_file():
                raise ValueError("找不到属于当前 Session 的进程。")
            record = json.loads(path.read_text(encoding="utf-8"))
        try:
            info = ProcessInfo(**record)
        except (TypeError, KeyError) as exc:
            raise ValueError("进程记录格式无效。") from exc
        if info.session_id != session_id or info.process_id != process_id:
            raise ValueError("进程记录归属不一致。")
        if info.status not in TERMINAL_STATUSES:
            info.status = "lost"
            info.exit_reason = "application_restarted"
            info.readiness = "unknown"
        return info

    def get(self, process_id: str, session_id: str) -> ProcessInfo:
        entry = self._processes.get(process_id)
        if entry is not None:
            return replace(self._entry(process_id, session_id).info)
        return self._saved_info(process_id, session_id)

    def list(self, session_id: str) -> list[ProcessInfo]:
        root = SessionPaths.for_session(self.project_root, session_id).root / "processes"
        validate_control_path(self.project_root, root)
        ids = {entry.info.process_id for entry in self._processes.values() if entry.info.session_id == session_id}
        authoritative = self._authoritative_processes(session_id)
        ids.update(authoritative)
        if root.is_dir():
            ids.update(path.name for path in root.iterdir() if path.is_dir())
        result = []
        for process_id in sorted(ids):
            try:
                if process_id in self._processes:
                    result.append(replace(self._entry(process_id, session_id).info))
                else:
                    result.append(self._saved_info(process_id, session_id, records=authoritative))
            except (ValueError, OSError, TypeError, json.JSONDecodeError):
                continue
        return result

    def read(self, process_id: str, session_id: str, *, cursor: int = 0,
             max_chars: int = 16000) -> OutputPage:
        self.get(process_id, session_id)
        budget = min(max_chars, self.limits.max_read_chars)
        entry = self._processes.get(process_id)
        if entry:
            return entry.output.read(cursor, max_chars=budget)
        path = SessionPaths.for_session(self.project_root, session_id).root / "processes" / process_id / "output.jsonl"
        validate_control_path(self.project_root, path)
        validate_control_path(self.project_root, path.with_name("output.index"))
        return read_saved_output(path, cursor, max_chars=budget)

    async def wait(self, process_id: str, session_id: str, *, timeout_ms: int = 1000) -> ProcessInfo:
        if not 0 <= timeout_ms <= 60000:
            raise ValueError("等待期限必须在 0 至 60000 毫秒之间。")
        if process_id not in self._processes:
            return self.get(process_id, session_id)
        entry = self._entry(process_id, session_id)
        try:
            await asyncio.wait_for(entry.done.wait(), timeout_ms / 1000)
        except TimeoutError:
            pass
        return replace(entry.info)

    async def write(self, process_id: str, session_id: str, text: str) -> None:
        if not isinstance(text, str) or len(text) > 65536:
            raise ValueError("进程输入必须是至多 65536 字符的字符串。")
        entry = self._entry(process_id, session_id)
        async with entry.io_lock:
            if session_id in self._stopping_sessions or (entry.info.lifetime == "turn" and
                    (session_id, entry.info.origin_turn_id) in self._stopping_turns):
                raise ValueError("执行范围已停止，不能再写入进程。")
            if entry.info.status != "running" or entry.backend is None:
                raise ValueError("进程没有可写的活动输入。")
            encoded = text.encode("utf-8")
            writing = asyncio.create_task(entry.backend.write(encoded))
            try:
                await asyncio.wait_for(asyncio.shield(writing), 10.0)
                entry.info.input_bytes += len(encoded)
                entry.info.input_status = "sent"
                # 输入日志只有成功字节数和状态，用户输入中的凭据不会复制到控制日志。
                self._emit(entry, "process.input_sent", strict=True)
            except asyncio.CancelledError:
                # 输入顺序由真实写入结束决定，不能让被取消的写线程越过下一次输入。
                # 先回收不读 stdin 的目标进程，才能解除真实管道写入的阻塞。
                entry.info.input_status = "cancelled"
                await _uninterrupted(asyncio.create_task(self.stop(process_id, session_id, reason="input_cancelled")))
                await asyncio.gather(writing, return_exceptions=True)
                raise
            except TimeoutError:
                entry.info.input_status = "timeout"
                await self.stop(process_id, session_id, reason="input_timeout")
                await asyncio.gather(writing, return_exceptions=True)
                raise RuntimeError("进程 10 秒内没有接收输入，已停止以解除阻塞。")
            except Exception:
                if writing.done() and not writing.cancelled() and writing.exception() is None:
                    # 字节已经发送但记录失败，不能重试或假装操作未发生。
                    await self.stop(process_id, session_id, reason="event_error")
                else:
                    entry.info.input_status = "failed"
                raise

    async def background(self, process_id: str, session_id: str) -> ProcessInfo:
        entry = self._entry(process_id, session_id)
        async with self._lock:
            if entry.info.status != "running" or entry.stop_task is not None:
                raise ValueError("停止中或已退出的进程不能转入后台。")
            if (session_id, entry.info.origin_turn_id) in self._stopping_turns:
                raise ValueError("轮次已停止，不能再转交进程。")
            if session_id in self._stopping_sessions:
                raise ValueError("Session 已停止，不能再转交进程。")
            previous = entry.info.lifetime
            entry.info.lifetime = "session"
            try:
                self._emit(entry, "process.backgrounded", strict=True)
            except Exception:
                entry.info.lifetime = previous
                asyncio.create_task(self.stop(process_id, session_id, reason="event_error"))
                raise
            return replace(entry.info)

    async def stop(self, process_id: str, session_id: str, *, reason: str = "cancelled") -> ProcessInfo:
        if process_id not in self._processes:
            return self.get(process_id, session_id)
        entry = self._entry(process_id, session_id)
        if entry.done.is_set():
            return replace(entry.info)
        if entry.stop_task is None:
            entry.stop_reason = reason
            entry.info.status = "stopping"
            self._emit(entry, "process.stopping")
            entry.stop_task = asyncio.create_task(self._stop_entry(entry))
        await _uninterrupted(entry.stop_task)
        return replace(entry.info)

    async def _stop_entry(self, entry: _Managed) -> None:
        # 创建窗口没有句柄；start 必须在取得句柄后负责取消收尾。
        while entry.backend is None and not entry.done.is_set():
            await asyncio.sleep(0.01)
        if entry.done.is_set():
            return
        try:
            await asyncio.wait_for(entry.backend.interrupt(), max(self.limits.stop_grace_seconds, .05))
        except (OSError, BrokenPipeError, TimeoutError):
            pass
        try:
            await asyncio.wait_for(entry.done.wait(), self.limits.stop_grace_seconds)
        except TimeoutError:
            await entry.backend.terminate()
            # monitor 排空时自带期限，真实系统退出确认后才能释放资源。
            await entry.done.wait()

    def seal_turn(self, session_id: str, turn_id: str | None) -> None:
        """同步关闭入口，用户停止后迟到的后台转交不能抢在异步清理前生效。"""
        self._stopping_turns.add((session_id, turn_id))

    def seal_session(self, session_id: str) -> None:
        self._stopping_sessions.add(session_id)

    async def stop_turn(self, session_id: str, turn_id: str | None) -> None:
        self.seal_turn(session_id, turn_id)
        ids = [entry.info.process_id for entry in self._processes.values()
               if entry.info.session_id == session_id and entry.info.origin_turn_id == turn_id
               and entry.info.lifetime == "turn" and not entry.done.is_set()]
        # 保护整组停止任务；取消不能在某个子任务首次运行前就将它跳过。
        await _uninterrupted(asyncio.gather(*(self.stop(process_id, session_id) for process_id in ids)))

    async def stop_session(self, session_id: str) -> None:
        self.seal_session(session_id)
        try:
            ids = [entry.info.process_id for entry in self._processes.values()
                   if entry.info.session_id == session_id and not entry.done.is_set()]
            await _uninterrupted(asyncio.gather(*(self.stop(process_id, session_id) for process_id in ids)))
        finally:
            self._stopping_sessions.discard(session_id)

    def active_session(self, session_id: str) -> bool:
        return any(entry.info.session_id == session_id and not entry.done.is_set()
                   for entry in self._processes.values())

    @property
    def active_count(self) -> int:
        return sum(not entry.done.is_set() for entry in self._processes.values())

    async def close(self) -> None:
        self._closed = True
        sessions = {entry.info.session_id for entry in self._processes.values() if not entry.done.is_set()}
        await asyncio.gather(*(self.stop_session(session_id) for session_id in sessions))
