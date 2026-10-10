from __future__ import annotations

import asyncio

import pytest

from lancher_code.mcp.config import MCPServerConfig
from lancher_code.mcp.connection import MCPServerConnection


async def test_close_propagates_caller_cancellation_while_connected_task_is_closing():
    connection = MCPServerConnection(MCPServerConfig('demo', 'stdio', command='unused'))
    closing = asyncio.Event()

    async def connection_task():
        await connection._close_event.wait()
        closing.set()
        await asyncio.Event().wait()

    connection._session = object()
    child = connection._task = asyncio.create_task(connection_task())
    caller = asyncio.create_task(connection.close())
    await asyncio.wait_for(closing.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert child.cancelled()
    assert connection._task is None


async def test_close_can_cancel_its_own_unready_connection_without_cancelling_caller():
    connection = MCPServerConnection(MCPServerConfig('demo', 'stdio', command='unused'))
    started = asyncio.Event()

    async def connection_task():
        started.set()
        await asyncio.Event().wait()

    child = connection._task = asyncio.create_task(connection_task())
    await asyncio.wait_for(started.wait(), 5)
    await connection.close()
    assert child.cancelled()
    assert connection._task is None
