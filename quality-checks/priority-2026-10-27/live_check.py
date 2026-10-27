"""Bounded live priority test. Call main(password); no credentials in saved results."""
import asyncio
import json
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

BASE = 'http://127.0.0.1:8088'
WS = 'ws://127.0.0.1:8088/v1/audio/speech/ws'
OUT = Path(__file__).resolve().parent

async def main(password):
    issued = []
    blockers = []
    tasks = []
    report = {}
    async with httpx.AsyncClient(base_url=BASE, timeout=60, trust_env=False) as admin:
        for _ in range(60):
            try:
                if (await admin.get('/health')).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        login = await admin.post('/admin/api/login', json={'username':'akashhandge','password':password})
        login.raise_for_status()
        admin.headers['X-CSRF-Token'] = login.json()['csrf_token']
        async def overview():
            response = await admin.get('/admin/api/overview')
            response.raise_for_status()
            return response.json()
        async def wait_queued(number):
            async with asyncio.timeout(10):
                while True:
                    data = await overview()
                    if data['queued_speech'] == number:
                        return data
                    await asyncio.sleep(.05)
        async def create(label, priority, concurrent=1):
            response = await admin.post('/admin/api/keys', json={
                'label':'Priority verification '+label, 'allowed_ips':['127.0.0.1/32'],
                'expires_days':1,'max_concurrent':concurrent,'requests_per_minute':60,'priority':priority})
            response.raise_for_status()
            key = response.json()
            issued.append(key)
            return key
        start = time.monotonic()
        admissions = []
        async def speech(label, key, websocket=False):
            payload = {'model':'speech','input':'Hello. Welcome.','voice':'en_female',
                       'stream':True,'response_format':'pcm'}
            count = 0
            if websocket:
                async with connect(WS, max_size=None) as socket:
                    await socket.send(json.dumps(dict(payload, api_key=key['key'])))
                    done = False
                    async for message in socket:
                        if isinstance(message, bytes):
                            count += len(message)
                        else:
                            event = json.loads(message)
                            if event['type'] == 'start':
                                admissions.append({'label':label,'time_s':time.monotonic()-start})
                            if event['type'] == 'error':
                                raise RuntimeError(event['message'])
                            if event['type'] == 'done':
                                done = True
                                break
                    assert done
            else:
                async with httpx.AsyncClient(timeout=60,trust_env=False) as client:
                    async with client.stream('POST',BASE+'/v1/audio/speech',
                            headers={'Authorization':'Bearer '+key['key']}, json=payload) as response:
                        response.raise_for_status()
                        admissions.append({'label':label,'time_s':time.monotonic()-start})
                        async for part in response.aiter_bytes():
                            count += len(part)
            assert count > 0 and count % 2 == 0
            return {'label':label,'bytes':count}
        try:
            state = await overview()
            assert state['active_speech'] == 0 and state['queued_speech'] == 0, 'Other speech traffic is active'
            assert state['concurrency_limit'] == 4
            block_key = await create('blockers',2,4)
            low = await create('Low',3)
            normal = await create('Normal',2)
            high = await create('High',1)
            cancel = await create('cancelled',3)
            # Verify PATCH round trip and rejected validation without touching real clients.
            rules = {'allowed_ips':['127.0.0.1/32'],'max_concurrent':1,'requests_per_minute':60}
            response = await admin.patch('/admin/api/keys/'+low['id'],json=dict(rules,priority=1))
            response.raise_for_status()
            keys = (await admin.get('/admin/api/keys')).json()['keys']
            assert next(k for k in keys if k['id']==low['id'])['priority'] == 1
            response = await admin.patch('/admin/api/keys/'+low['id'],json=dict(rules,priority=4))
            assert response.status_code == 422
            response = await admin.patch('/admin/api/keys/'+low['id'],json=dict(rules,priority=3))
            response.raise_for_status()
            report['priority_edit_and_validation'] = 'passed'
            long_text = ('Welcome to our service. This is a temporary speech scheduling check. '*12)
            for _ in range(4):
                socket = await connect(WS, max_size=None, close_timeout=3)
                blockers.append(socket)
                await socket.send(json.dumps({'api_key':block_key['key'],'voice':'en_female','input':long_text}))
                event = json.loads(await socket.recv())
                assert event['type'] == 'start', event
            assert (await overview())['active_speech'] == 4
            for number, (label, key, websocket) in enumerate([
                    ('Low',low,False), ('Normal',normal,True), ('High',high,False)], 1):
                tasks.append(asyncio.create_task(speech(label,key,websocket)))
                await wait_queued(number)
            queued = await overview()
            assert queued['queued_by_priority'] == {'high':1,'normal':1,'low':1}
            report['queue_by_priority'] = queued['queued_by_priority']
            # A disconnected waiter must disappear before admission.
            socket = await connect(WS, max_size=None, close_timeout=3)
            await socket.send(json.dumps({'api_key':cancel['key'],'voice':'en_female','input':'Hello.'}))
            await wait_queued(4)
            await socket.close()
            await wait_queued(3)
            report['queued_disconnect_cleanup'] = 'passed'
            # Free just one slot. The other three requests continue uninterrupted.
            await blockers[0].close()
            report['completed'] = await asyncio.wait_for(asyncio.gather(*tasks), 40)
            report['admissions'] = admissions
            assert [event['label'] for event in admissions] == ['High','Normal','Low'], admissions
            report['priority_order'] = 'passed'
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(socket.close() for socket in blockers),return_exceptions=True)
            for key in issued:
                response = await admin.post('/admin/api/keys/'+key['id']+'/revoke')
                response.raise_for_status()
                response = await admin.delete('/admin/api/keys/'+key['id'])
                response.raise_for_status()
            async with asyncio.timeout(15):
                while True:
                    state = await overview()
                    if state['active_speech'] == state['queued_speech'] == 0:
                        break
                    await asyncio.sleep(.1)
            report['final_active'] = state['active_speech']
            report['final_queued'] = state['queued_speech']
            await admin.post('/admin/api/logout')
            (OUT/'live-report.json').write_text(json.dumps(report,indent=2))
            print(json.dumps(report,indent=2),flush=True)
