"""One owning async task keeps the MCP transport and session alive.

Calls queue with deadlines; failed calls are not replayed automatically. A broken
session is closed, and the next caller triggers discovery on a new session.
"""
import asyncio
from concurrent.futures import Future, TimeoutError
import json
import queue
import threading
from time import monotonic
from ..config import get_settings


class Worker:
    def __init__(self, settings):
        self.settings = settings.model_copy(deep=True)
        self.queue = queue.Queue(maxsize=64)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True, name='mcp-session')
        self.thread.start()

    async def run(self):
        from datetime import timedelta
        from mcp import ClientSession
        from .mcp_adapter import _transport, _invoke
        import anyio
        while not self.stopped.is_set():
            item = await asyncio.to_thread(self.queue.get)
            if item is None:
                return
            future, name, args, end = item
            if future.cancelled() or end <= monotonic():
                continue
            try:
                async with _transport(self.settings) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.settings.mcp_timeout)) as session:
                        with anyio.fail_after(max(.001, end-monotonic())):
                            await session.initialize()
                            discovered, cursor = {}, None
                            for _ in range(10):
                                page = await session.list_tools(cursor=cursor)
                                discovered.update({tool.name: tool for tool in page.tools})
                                cursor = page.nextCursor
                                if not cursor:
                                    break
                        while item is not None:
                            future, name, args, end = item
                            if not future.cancelled() and end > monotonic():
                                with anyio.fail_after(max(.001, end-monotonic())):
                                    result = await _invoke(session, discovered, name, args)
                                if not future.done():
                                    future.set_result(result)
                            if self.stopped.is_set():
                                return
                            item = await asyncio.to_thread(self.queue.get)
                        return
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    def call(self, name, args, timeout):
        future = Future()
        try:
            self.queue.put_nowait((future, name, args, monotonic()+timeout))
        except queue.Full as exc:
            raise TimeoutError('MCP session queue full') from exc
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def close(self):
        self.stopped.set()
        while True:
            try:
                item = self.queue.get_nowait()
                if item:
                    item[0].cancel()
            except queue.Empty:
                break
        self.queue.put_nowait(None)
        self.thread.join(timeout=2)


_lock = threading.Lock()
_worker = None
_identity = None


def invoke(name, args, timeout):
    global _worker, _identity
    settings = get_settings()
    identity = json.dumps({key: value for key, value in settings.model_dump().items() if key.startswith('mcp_')}, sort_keys=True)
    with _lock:
        if _worker is None or _identity != identity or not _worker.thread.is_alive():
            if _worker:
                _worker.close()
            _worker, _identity = Worker(settings), identity
        worker = _worker
    return worker.call(name, args, timeout)


def close():
    global _worker, _identity
    with _lock:
        if _worker:
            _worker.close()
        _worker = _identity = None
