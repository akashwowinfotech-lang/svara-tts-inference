"""Startup inference and cancellable idle-only model maintenance."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable


class ModelWarmup:
    def __init__(self, name: str, run: Callable[[bool], Awaitable[bool | None]],
                 busy: Callable[[], bool], interval_seconds: float = 120,
                 timeout_seconds: float = 30, startup_timeout_seconds: float = 120):
        self.name, self.run, self.busy = name, run, busy
        self.interval = max(0.0, interval_seconds)
        self.timeout, self.startup_timeout = timeout_seconds, startup_timeout_seconds
        self.last_activity = time.monotonic()
        self.ready = False
        self.last_success = None
        self.runs = self.failures = self.skipped = 0
        self._current = self._loop_task = None
        self._closing = False
        self.logger = logging.getLogger(name + '.warmup')

    def touch(self):
        self.last_activity = time.monotonic()

    def status(self) -> dict:
        return {'ready': self.ready, 'running': bool(self._current and not self._current.done()),
                'last_success': self.last_success, 'runs': self.runs,
                'failures': self.failures, 'skipped': self.skipped,
                'idle_interval_seconds': self.interval}

    async def start(self):
        await self._execute(startup=True)
        self.ready = True
        if self.interval:
            self._loop_task = asyncio.create_task(self._loop(), name=self.name + '-keep-warm')

    async def before_request(self):
        """Customer work cancels maintenance before entering inference."""
        self.touch()
        task = self._current
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _execute(self, startup: bool):
        started = time.monotonic()
        task = asyncio.create_task(self.run(startup), name=self.name + '-warmup-pass')
        self._current = task
        try:
            result = await asyncio.wait_for(task, self.startup_timeout if startup else self.timeout)
            if result is False:
                if startup:
                    raise RuntimeError('Startup warmup was skipped')
                self.skipped += 1
                return
            self.last_success = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self.runs += 1
            self.logger.info('%s warmup completed in %.3fs', 'Startup' if startup else 'Idle', time.monotonic() - started)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failures += 1
            self.logger.exception('%s warmup failed', 'Startup' if startup else 'Idle')
            if startup:
                raise
        finally:
            if self._current is task:
                self._current = None
            self.touch()

    async def _tick(self):
        if self.busy():
            self.skipped += 1
            self.touch()
            return
        if time.monotonic() - self.last_activity >= self.interval:
            await self._execute(startup=False)

    async def _loop(self):
        while not self._closing:
            await asyncio.sleep(min(15.0, self.interval))
            try:
                await self._tick()
            except asyncio.CancelledError:
                if self._closing or asyncio.current_task().cancelling():
                    raise
                # Only the child pass was cancelled by an arriving request.

    async def close(self):
        self._closing = True
        tasks = [task for task in (self._current, self._loop_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def finish_inference_thread(function):
    """A cancelled maintenance pass still waits for its GPU thread to finish."""
    future = asyncio.get_running_loop().run_in_executor(None, function)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # Multiple arriving requests or shutdown may cancel the same task.
        # Never release model ownership while its worker is still running.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                pass
        future.result()
        raise
