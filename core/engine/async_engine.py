"""Dedicated asyncio event loop running in its own thread.

Discovery (254 hosts) and updates (parallel flashes) are I/O-bound; async fits
naturally. The GUI thread never blocks. Submit coroutines via :meth:`submit`;
the returned ``concurrent.futures.Future`` resolves on the engine thread, and
results cross back to the Qt main thread via Qt signals (queued connections).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Coroutine

log = logging.getLogger(__name__)


class AsyncEngine:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="AsyncEngine", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("AsyncEngine failed to start within 5 s")
        log.info("AsyncEngine started")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:  # pragma: no cover
                pass

    def submit(self, coro: Coroutine) -> Future:
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("AsyncEngine not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_soon(self, fn, *args) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        if self._loop is None:
            return
        if self._loop.is_running():
            # Cancel any in-flight tasks first so they don't fire
            # "Task was destroyed but it is pending!" on shutdown.
            async def _drain() -> None:
                pending = [
                    t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task()
                ]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            try:
                fut = asyncio.run_coroutine_threadsafe(_drain(), self._loop)
                fut.result(timeout=2)
            except Exception:
                log.warning("AsyncEngine task drain timed out", exc_info=False)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        log.info("AsyncEngine stopped")
