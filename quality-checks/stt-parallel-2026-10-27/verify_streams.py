import asyncio,json,time,sys,importlib.util
from pathlib import Path
import httpx
from websockets.asyncio.client import connect
STT=Path('/home/wowbharat/WOWBHARAT/STT');sys.path.insert(0,str(STT))
from server.access import AccessStore
ROOT=Path('/home/wowbharat/WOWBHARAT/TTS/upstream-svara')
OUT=ROOT/'quality-checks/stt-parallel-2026-10-27'
spec=importlib.util.spec_from_file_location('audit',ROOT/'quality-checks/concurrency-2026-10-27/benchmark.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
store=AccessStore(str(STT/'.data/access.sqlite3'),(STT/'.secrets/access.key').read_text().strip())
key=store.create_key('STT parallel streaming validation',['127.0.0.1/32','103.183.80.247/32'],1,16,600)
audit.KEYS['stt']=key;audit.WAV=(ROOT/'quality-checks/concurrency-2026-10-27/fixture.wav').read_bytes()

async def main():
 data={}
 async with httpx.AsyncClient(base_url='https://api.stt.ganas.ai',timeout=60) as client:
  for count in (2,4):
   before=(await client.get('/ready')).json();go=asyncio.Event()
   tasks=[asyncio.create_task(audit.stt_ws(i,go)) for i in range(count)]
   go.set();rows=await asyncio.gather(*tasks)
   after=(await client.get('/ready')).json()
   data[f'wss_{count}']={'results':rows,'batch_passes':after['completed_batch_passes']-before['completed_batch_passes']}
   print('WSS',count,'batch passes',data[f'wss_{count}']['batch_passes'],'post audio',[r.get('post_audio_s') for r in rows],flush=True)
   assert all(r['ok'] for r in rows),rows
  go=asyncio.Event()
  async def delayed_http():
   await go.wait();await asyncio.sleep(5)
   start=time.perf_counter()
   r=await client.post('/v1/audio/transcriptions',headers={'Authorization':'Bearer '+key['key']},files={'file':('sample.wav',audit.WAV,'audio/wav')})
   return {'status':r.status_code,'seconds':round(time.perf_counter()-start,3),'body':r.json()}
  streams=[asyncio.create_task(audit.stt_ws(i,go)) for i in range(2)]
  uploads=[asyncio.create_task(delayed_http()) for _ in range(2)]
  go.set();mixed=await asyncio.gather(*streams,*uploads)
  data['mixed_protocols']=mixed
  assert all(r.get('ok',r.get('status')==200) for r in mixed),mixed
  data['ready']=(await client.get('/ready')).json()
  assert data['ready']['inference_batch_size']==2 and data['ready']['batch_memory_fallbacks']==0
  assert data['wss_4']['batch_passes']>0
  print('Mixed HTTP/WSS passed; ready:',data['ready'],flush=True)
  (OUT/'streams.json').write_text(json.dumps(data,indent=2,ensure_ascii=False))
try:asyncio.run(main())
finally:store.revoke_key(key['id'])
