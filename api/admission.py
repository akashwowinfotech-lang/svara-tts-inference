"""Cancel queued speech when its client disconnects; recheck access on admission."""
from __future__ import annotations

import asyncio

from api.access import AccessStore, ClientKey, RequestLimiter


class ClientWentAway(Exception):
    pass


async def http_disconnect(request):
    while not await request.is_disconnected():
        await asyncio.sleep(.1)


async def websocket_disconnect(websocket):
    while True:
        if (await websocket.receive())['type'] == 'websocket.disconnect':
            return


async def admit_speech(limiter: RequestLimiter, key: ClientKey, store: AccessStore,
                       address: str, disconnected) -> ClientKey:
    async def reserve():
        await limiter.acquire(key)
        return True

    admission = asyncio.create_task(reserve())
    watcher = asyncio.create_task(disconnected)
    try:
        finished, _ = await asyncio.wait((admission, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in finished:
            await watcher
            raise ClientWentAway()
        await admission
        refreshed = store.refresh_client(key, address)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    except BaseException:
        watcher.cancel()
        if not admission.done():
            admission.cancel()
        outcomes = await asyncio.gather(admission, watcher, return_exceptions=True)
        if outcomes[0] is True:
            await limiter.release(key)
        raise
    return refreshed
