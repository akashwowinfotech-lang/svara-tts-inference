import sys,asyncio,json,time,io,wave,subprocess
from pathlib import Path
import httpx
from websockets.asyncio.client import connect
STT=Path('/home/wowbharat/WOWBHARAT/STT');sys.path.insert(0,str(STT))
from server.access import AccessStore
ROOT=Path('/home/wowbharat/WOWBHARAT/TTS/upstream-svara')
OUT=ROOT/'quality-checks/stt-parallel-2026-10-27'
PHASE=sys.argv[1];issued=None
store=AccessStore(str(STT/'.data/access.sqlite3'),(STT/'.secrets/access.key').read_text().strip())
fixtures={
'en':ROOT/'quality-checks/concurrency-2026-10-27/fixture.wav',
'mr':ROOT/'quality-checks/2026-10-15/after-mr_female-1.0.wav',
'kn':ROOT/'quality-checks/2026-10-15/preset-kn_female-1.0.wav',
 'te':ROOT/'quality-checks/2026-10-15/preset-te_male-1.0.wav'}
AUDIO={}
for language,path in fixtures.items():
 raw=subprocess.run(['ffmpeg','-v','error','-i',str(path),'-t','8','-ar','16000','-ac','1','-f','s16le','pipe:1'],capture_output=True,check=True).stdout
 out=io.BytesIO()
 with wave.open(out,'wb') as f:f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(raw)
 AUDIO[language]=out.getvalue()

async def request(client,lang,go=None):
 if go:await go.wait()
 t=time.perf_counter();r=await client.post('/v1/audio/transcriptions',headers={'Authorization':'Bearer '+issued['key']},files={'file':('sample.wav',AUDIO[lang],'audio/wav')})
 body=r.json();return {'language':lang,'status':r.status_code,'seconds':round(time.perf_counter()-t,4),'body':body}

async def simultaneous(client,langs):
 go=asyncio.Event();tasks=[asyncio.create_task(request(client,l,go)) for l in langs]
 go.set();return await asyncio.gather(*tasks)

async def main():
 async with httpx.AsyncClient(base_url='https://api.stt.ganas.ai',timeout=120) as c:
  if PHASE=='before':
   reference=[await request(c,l) for l in AUDIO]
   loads=[]
   for n in (1,2,4):
    rows=await simultaneous(c,['en']*n);loads.append(rows);print('before',n,[r['seconds'] for r in rows],flush=True)
   data={'reference':reference,'loads':loads};(OUT/'before.json').write_text(json.dumps(data,indent=2,ensure_ascii=False))
   assert all(r['status']==200 and r['body'].get('text') for r in reference)
  else:
   before=json.loads((OUT/'before.json').read_text());refs={r['language']:r['body']['text'] for r in before['reference']}
   rows=await simultaneous(c,list(AUDIO))
   matching=[r['status']==200 and r['body'].get('text')==refs[r['language']] for r in rows]
   data={'reference_comparison':rows,'exact_reference_matches':matching,'loads':[]}
   print('exact transcript matches',matching,flush=True)
   for n in (1,2,4,6):
    batch=await simultaneous(c,['en']*n);data['loads'].append(batch);print('after',n,[(r['status'],r['seconds']) for r in batch],flush=True)
   data['ready']=(await c.get('/ready')).json()
   (OUT/'after.json').write_text(json.dumps(data,indent=2,ensure_ascii=False))
   print('ready',data['ready'],flush=True)
   assert all(matching),'Batched output changed compared to the current service; review before activation'
   assert all(r['status']==200 for b in data['loads'] for r in b),'A benchmark request failed'
   assert data['ready']['inference_batch_size']==2 and data['ready']['batch_memory_fallbacks']==0,'Batching ran out of memory'
   assert data['ready']['completed_batch_passes']>0,'No real batched inference occurred'
   baseline=max(r['seconds'] for r in before['loads'][-1]);new=max(r['seconds'] for r in data['loads'][2])
   assert new<baseline*1.1, f'Four-request latency regressed: {baseline} -> {new}'

try:
 issued=store.create_key('STT parallel validation 2026-10-27',['127.0.0.1/32','103.183.80.247/32'],1,16,600)
 asyncio.run(main())
finally:
 if issued:store.revoke_key(issued['id'])
