"""Priority admission, bounded waiting and cleanup without loading the model."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from api.access import AccessDenied, AccessStore, ClientKey, RequestLimiter
from api.admission import ClientWentAway, admit_speech


def client(name, priority=2, concurrent=1, rpm=100):
    return ClientKey(name, name, [], concurrent, rpm, priority)


class PriorityStoreTests(unittest.TestCase):
    def test_existing_database_migrates_and_priority_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'access.sqlite3')
            # Simulate the complete pre-priority table; AccessStore creates the rest.
            with sqlite3.connect(path) as db:
                db.execute('''CREATE TABLE client_keys(id TEXT PRIMARY KEY,label TEXT NOT NULL,
                    secret_hash TEXT NOT NULL UNIQUE,prefix TEXT NOT NULL,allowed_ips TEXT NOT NULL,
                    max_concurrent INTEGER NOT NULL,requests_per_minute INTEGER NOT NULL,
                    created_at TEXT NOT NULL,expires_at TEXT,revoked_at TEXT)''')
                db.execute("INSERT INTO client_keys VALUES('legacy','Legacy','hash','spk_x','[]',2,60,'2026-01-01',NULL,NULL)")
            store = AccessStore(path, 'a'*48)
            self.assertEqual(store.list_keys()[0]['priority'], 2)
            key = store.create_key('Premium', [], 1, 2, 60, priority=1)
            authenticated = store.authenticate(key['key'], '127.0.0.1')
            self.assertEqual(authenticated.priority, 1)
            self.assertTrue(store.update_key(key['id'], [], 2, 60))
            self.assertEqual(store.refresh_client(authenticated, '127.0.0.1').priority, 1)
            store.update_key(key['id'], [], 2, 60, priority=3)
            reloaded = AccessStore(path, 'a'*48)
            self.assertEqual(reloaded.authenticate(key['key'], '127.0.0.1').priority, 3)
            store.revoke_key(key['id'])
            with self.assertRaises(AccessDenied):
                store.refresh_client(authenticated, '127.0.0.1')
            for priority in (0, 4, True, '1'):
                with self.assertRaises(ValueError):
                    store.create_key('Invalid', [], 1, 2, 60, priority)


class PriorityQueueTests(unittest.IsolatedAsyncioTestCase):
    async def wait_pending(self, limiter, number):
        async with asyncio.timeout(1):
            while len(limiter.pending) != number:
                await asyncio.sleep(0)

    async def test_high_before_low_fifo_ties_without_preempting_active(self):
        limiter = RequestLimiter(1)
        active = client('active')
        await limiter.acquire(active)
        order = []
        async def worker(key):
            await limiter.acquire(key)
            order.append(key.id)
            await limiter.release(key)
        tasks = []
        for key in (client('low', 3), client('normal', 2), client('high-first', 1), client('high-second', 1)):
            tasks.append(asyncio.create_task(worker(key)))
            await self.wait_pending(limiter, len(tasks))
        self.assertEqual(order, [])
        self.assertEqual(limiter.active_total, 1)
        await limiter.release(active)
        await asyncio.gather(*tasks)
        self.assertEqual(order, ['high-first', 'high-second', 'normal', 'low'])
        self.assertEqual(limiter.active_total, 0)

    async def test_busy_key_does_not_block_another_key(self):
        limiter = RequestLimiter(2)
        first, second = client('first', 1), client('second', 3)
        await limiter.acquire(first)
        waiting = asyncio.create_task(limiter.acquire(first))
        await self.wait_pending(limiter, 1)
        await asyncio.wait_for(limiter.acquire(second), .5)
        self.assertEqual(limiter.active_total, 2)
        await limiter.release(second)
        self.assertFalse(waiting.done())
        await limiter.release(first)
        await waiting
        await limiter.release(first)

    async def test_waiting_requests_age_ahead_of_new_high_priority(self):
        limiter = RequestLimiter(1, aging_seconds=10)
        active, low, high = client('active'), client('low', 3), client('high', 1)
        await limiter.acquire(active)
        old = asyncio.create_task(limiter.acquire(low))
        await self.wait_pending(limiter, 1)
        limiter.pending[0].created -= 31
        new = asyncio.create_task(limiter.acquire(high))
        await self.wait_pending(limiter, 2)
        await limiter.release(active)
        await old
        self.assertFalse(new.done())
        await limiter.release(low)
        await new
        await limiter.release(high)

    async def test_full_queue_and_per_key_queue_are_bounded(self):
        limiter = RequestLimiter(1, max_queue=2, per_key_queue=1)
        active, first, second = client('active'), client('first'), client('second')
        await limiter.acquire(active)
        one = asyncio.create_task(limiter.acquire(first))
        await self.wait_pending(limiter, 1)
        with self.assertRaises(AccessDenied):
            await limiter.acquire(first)
        two = asyncio.create_task(limiter.acquire(second))
        await self.wait_pending(limiter, 2)
        with self.assertRaises(AccessDenied):
            await limiter.acquire(client('third'))
        one.cancel(); two.cancel()
        await asyncio.gather(one, two, return_exceptions=True)
        self.assertEqual(limiter.status()['queued_speech'], 0)
        await limiter.release(active)
        self.assertEqual(limiter.active_total, 0)

    async def test_timeout_does_not_leak_a_slot(self):
        limiter = RequestLimiter(1, wait_timeout=.02)
        active, waiting = client('active'), client('waiting')
        await limiter.acquire(active)
        with self.assertRaisesRegex(AccessDenied, 'timed out'):
            await limiter.acquire(waiting)
        self.assertEqual(limiter.active_total, 1)
        self.assertEqual(limiter.pending, [])
        await limiter.release(active)
        await limiter.acquire(waiting)
        await limiter.release(waiting)

    async def test_cancel_just_after_dispatch_releases_reserved_slot(self):
        limiter = RequestLimiter(1)
        active, waiting = client('active'), client('waiting')
        await limiter.acquire(active)
        task = asyncio.create_task(limiter.acquire(waiting))
        await self.wait_pending(limiter, 1)
        await limiter.release(active)  # Reserves a slot, before waiter resumes.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(limiter.active_total, 0)
        self.assertEqual(limiter.pending, [])

    async def test_rate_limit_also_counts_queued_requests(self):
        limiter = RequestLimiter(1)
        active, waiting = client('active'), client('waiting', rpm=1)
        await limiter.acquire(active)
        task = asyncio.create_task(limiter.acquire(waiting))
        await self.wait_pending(limiter, 1)
        with self.assertRaisesRegex(AccessDenied, 'rate limit'):
            await limiter.acquire(waiting)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await limiter.release(active)

    async def test_status_counts_waiting_priorities(self):
        limiter = RequestLimiter(1)
        active = client('active')
        await limiter.acquire(active)
        tasks = [asyncio.create_task(limiter.acquire(client(str(priority), priority))) for priority in (1,2,3)]
        await self.wait_pending(limiter, 3)
        self.assertEqual(limiter.status()['queued_by_priority'], {'high':1, 'normal':1, 'low':1})
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await limiter.release(active)

    async def test_disconnected_queued_client_is_removed(self):
        limiter = RequestLimiter(1)
        active, waiting = client('active'), client('waiting')
        await limiter.acquire(active)
        disconnected = asyncio.Event()
        task = asyncio.create_task(admit_speech(limiter, waiting, None, '127.0.0.1', disconnected.wait()))
        await self.wait_pending(limiter, 1)
        disconnected.set()
        with self.assertRaises(ClientWentAway):
            await task
        self.assertEqual(limiter.pending, [])
        self.assertEqual(limiter.active_total, 1)
        await limiter.release(active)

    async def test_revocation_during_wait_releases_admitted_slot(self):
        limiter = RequestLimiter(1)
        with tempfile.TemporaryDirectory() as directory:
            store = AccessStore(str(Path(directory)/'db'), 'a'*48)
            issued = store.create_key('Waiting', [], 1, 1, 60)
            key = store.authenticate(issued['key'], '127.0.0.1')
            active = client('active')
            await limiter.acquire(active)
            task = asyncio.create_task(admit_speech(limiter, key, store, '127.0.0.1', asyncio.Event().wait()))
            await self.wait_pending(limiter, 1)
            store.revoke_key(key.id)
            await limiter.release(active)
            with self.assertRaises(AccessDenied):
                await task
            self.assertEqual(limiter.active_total, 0)

    async def test_outer_cancellation_during_success_cleanup_releases_slot(self):
        limiter = RequestLimiter(1)
        key = client('test')
        class Store:
            def refresh_client(self, *args):
                return key
        entered_cleanup = asyncio.Event()
        async def watcher():
            try:
                await asyncio.Event().wait()
            finally:
                entered_cleanup.set()
                await asyncio.sleep(.01)
        task = asyncio.create_task(admit_speech(limiter, key, Store(), '127.0.0.1', watcher()))
        await entered_cleanup.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(limiter.active_total, 0)


if __name__ == '__main__':
    unittest.main()
