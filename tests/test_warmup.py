"""Lifecycle checks for maintenance priority and non-overlapping inference."""
import asyncio
import threading
import time
import unittest

from api.warmup import ModelWarmup, finish_inference_thread


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_must_finish_before_ready_and_failure_stays_unready(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def run(startup):
            self.assertTrue(startup)
            entered.set()
            await release.wait()

        warmer = ModelWarmup('test', run, lambda: False, interval_seconds=0)
        task = asyncio.create_task(warmer.start())
        await entered.wait()
        self.assertFalse(warmer.ready)
        release.set()
        await task
        self.assertTrue(warmer.ready)
        self.assertEqual(warmer.runs, 1)
        await warmer.close()

        async def skipped(startup):
            return False

        warmer = ModelWarmup('test', skipped, lambda: False, interval_seconds=0)
        with self.assertLogs('test.warmup', level='ERROR'):
            with self.assertRaises(RuntimeError):
                await warmer.start()
        self.assertFalse(warmer.ready)

    async def test_busy_and_recent_traffic_prevent_idle_inference(self):
        calls = []
        busy = True

        async def run(startup):
            calls.append(startup)

        warmer = ModelWarmup('test', run, lambda: busy, interval_seconds=100)
        await warmer.start()
        warmer.last_activity = time.monotonic() - 200
        await warmer._tick()
        busy = False
        await warmer._tick()
        self.assertEqual(calls, [True])
        warmer.last_activity -= 200
        await warmer._tick()
        self.assertEqual(calls, [True, False])
        await warmer.close()

    async def test_customer_cancels_idle_pass_and_scheduler_survives(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def run(startup):
            if startup:
                return
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        warmer = ModelWarmup('test', run, lambda: False, interval_seconds=.01)
        await warmer.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await warmer.before_request()
            self.assertTrue(cancelled.is_set())
            await asyncio.sleep(0)
            self.assertFalse(warmer._loop_task.done())
            self.assertEqual(warmer.failures, 0)
        finally:
            await warmer.close()
        self.assertTrue(warmer._loop_task.done())

    async def test_repeated_cancellation_waits_for_gpu_thread(self):
        entered, release = threading.Event(), threading.Event()

        def infer():
            entered.set()
            release.wait(2)

        task = asyncio.create_task(finish_inference_thread(infer))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_idle_failure_is_reported_and_next_pass_can_recover(self):
        fail = True

        async def run(startup):
            if not startup and fail:
                raise RuntimeError('inference failed')

        warmer = ModelWarmup('test', run, lambda: False, interval_seconds=0)
        await warmer.start()
        with self.assertLogs('test.warmup', level='ERROR'):
            await warmer._tick()
        self.assertTrue(warmer.ready)
        self.assertEqual(warmer.failures, 1)
        fail = False
        await warmer._tick()
        self.assertEqual(warmer.runs, 2)
        await warmer.close()


if __name__ == '__main__':
    unittest.main()
