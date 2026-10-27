import asyncio,importlib.util,sys,json,time,statistics,subprocess,io,wave,math
from pathlib import Path
import httpx
from websockets.asyncio.client import connect

OUT=Path('/home/wowbharat/WOWBHARAT/TTS/upstream-svara/quality-checks/concurrency-2026-10-27')
TTS=Path('/home/wowbharat/WOWBHARAT/TTS/upstream-svara')
STT=Path('/home/wowbharat/WOWBHARAT/STT')
KEYS={}; STORES={}; RESULTS=[]; PCM=b''; WAV=b''

def setup():
    for name,root,relative,secret in [('tts',TTS,'api/access.py','.secrets/admin.key'),('stt',STT,'server/access.py','.secrets/access.key')]:
        spec=importlib.util.spec_from_file_location('audit_'+name,root/relative)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        store=module.AccessStore(str(root/'.data/access.sqlite3'),(root/secret).read_text().strip())
        issued=store.create_key('Parallel request audit 2026-10-27', ['127.0.0.1/32','103.183.80.247/32'],1,16,600)
        STORES[name]=store;KEYS[name]=issued

def pct(xs,p):
    return round(sorted(xs)[min(len(xs)-1,math.ceil(len(xs)*p)-1)],3) if xs else None

def summary(label,rows,wall):
    good=[r for r in rows if r['ok']]
    report={'label':label,'requests':len(rows),'success':len(good),'errors':[r for r in rows if not r['ok']], 'wall_s':round(wall,3),'results':rows}
    for metric in ['first_s','total_s','post_audio_s','queue_s','infer_s','underrun_s']:
        values=[r[metric] for r in good if metric in r and r[metric] is not None]
        if values: report[metric]={'p50':round(statistics.median(values),3),'max':round(max(values),3),'p95':pct(values,.95)}
    RESULTS.append(report);(OUT/'results.json').write_text(json.dumps(RESULTS,indent=2,ensure_ascii=False))
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False),flush=True)

async def tts_one(i,go,capture=False,http=False):
    global PCM
    await go.wait();start=time.perf_counter();first=None;audio=[];playback_end=None;gap=0
    payload={'input':'Hello, welcome to Ganas. Please tell me how I can help you today.','voice':'en_female','model':'speech','response_format':'pcm','stream':True}
    def on_audio(data):
        nonlocal first,playback_end,gap
        now=time.perf_counter();first=first or now;audio.append(data)
        if playback_end is None:playback_end=now+.12
        if now>playback_end:gap+=now-playback_end;playback_end=now+.12
        playback_end+=len(data)/48000
    try:
        async with asyncio.timeout(100):
            if http:
                async with httpx.AsyncClient(timeout=100) as client:
                    async with client.stream('POST','https://api.tts.ganas.ai/v1/audio/speech',headers={'Authorization':'Bearer '+KEYS['tts']['key']},json=payload) as r:
                        if r.status_code!=200:return {'ok':False,'status':r.status_code,'total_s':round(time.perf_counter()-start,3)}
                        async for chunk in r.aiter_bytes():
                            if chunk:on_audio(chunk)
            else:
                async with connect('wss://api.tts.ganas.ai/v1/audio/speech/ws',max_size=None,open_timeout=10) as ws:
                    await ws.send(json.dumps(dict(payload,api_key=KEYS['tts']['key'])))
                    async for msg in ws:
                        if isinstance(msg,bytes):on_audio(msg)
                        else:
                            event=json.loads(msg)
                            if event.get('type')=='error':return {'ok':False,'error':event.get('message'),'total_s':round(time.perf_counter()-start,3)}
                            if event.get('type')=='done':break
        data=b''.join(audio)
        if capture:PCM=data;(OUT/'fixture.pcm').write_bytes(data)
        return {'ok':bool(data),'first_s':round(first-start,3) if first else None,'total_s':round(time.perf_counter()-start,3),'audio_s':round(len(data)/48000,3),'underrun_s':round(gap,3)}
    except Exception as e:return {'ok':False,'error':type(e).__name__+': '+str(e)[:160],'total_s':round(time.perf_counter()-start,3)}

async def stt_http(i,go):
    await go.wait();start=time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r=await client.post('https://api.stt.ganas.ai/v1/audio/transcriptions',headers={'Authorization':'Bearer '+KEYS['stt']['key']},files={'file':('audit.wav',WAV,'audio/wav')})
            body=r.json()
        return {'ok':r.status_code==200 and bool(body.get('text')),'status':r.status_code,'total_s':round(time.perf_counter()-start,3),'server_s':body.get('duration_seconds'),'audio_s':body.get('audio_seconds'),'text':body.get('text'),'disallowed':body.get('is_disallowed_script')}
    except Exception as e:return {'ok':False,'error':type(e).__name__+': '+str(e)[:160]}

async def stt_ws(i,go):
    await go.wait();start=time.perf_counter();events=[];ended=None
    try:
        async with asyncio.timeout(120):
            async with connect('wss://api.stt.ganas.ai/v1/audio/transcriptions/ws',open_timeout=15,max_size=None) as ws:
                await ws.send(json.dumps({'api_key':KEYS['stt']['key']}));ready=json.loads(await ws.recv())
                if ready.get('event')!='ready':return {'ok':False,'error':str(ready)}
                async def receive():
                    async for msg in ws:
                        e=json.loads(msg)
                        if e.get('event')=='error':raise RuntimeError(e.get('detail'))
                        if e.get('event')=='transcript' and e.get('text'):events.append((time.perf_counter(),e))
                        if e.get('event')=='flushed':return
                receiver=asyncio.create_task(receive())
                try:
                    with wave.open(io.BytesIO(WAV),'rb') as f:raw=f.readframes(f.getnframes())
                    send_start=time.perf_counter()
                    for pos in range(0,len(raw),3200):
                        await ws.send(raw[pos:pos+3200]);await asyncio.sleep(max(0,send_start+(pos+3200)/32000-time.perf_counter()))
                    ended=time.perf_counter();await ws.send(json.dumps({'event':'end'}));await receiver
                finally:
                    if not receiver.done():receiver.cancel()
                    await asyncio.gather(receiver,return_exceptions=True)
        e=events[-1][1] if events else {}
        return {'ok':bool(events),'total_s':round(time.perf_counter()-start,3),'post_audio_s':round(max(0,events[-1][0]-ended),3) if events else None,'queue_s':e.get('queue_wait_seconds'),'infer_s':e.get('inference_seconds'),'text':e.get('text'),'disallowed':e.get('is_disallowed_script')}
    except Exception as e:return {'ok':False,'error':type(e).__name__+': '+str(e)[:160],'total_s':round(time.perf_counter()-start,3)}

async def health_probe(stop,records):
    async with httpx.AsyncClient(timeout=5) as c:
        while not stop.is_set():
            t=time.perf_counter()
            try:
                rs=await asyncio.gather(c.get('https://api.tts.ganas.ai/health'),c.get('https://api.stt.ganas.ai/health'))
                records.append({'seconds':round(time.perf_counter()-t,3),'ok':all(r.status_code==200 for r in rs)})
            except Exception as e:records.append({'ok':False,'error':type(e).__name__})
            await asyncio.sleep(.5)

async def phase(label,fn,count,capture=False):
    go=asyncio.Event();stop=asyncio.Event();probes=[];probe=asyncio.create_task(health_probe(stop,probes))
    tasks=[asyncio.create_task(fn(i,go,True) if capture else fn(i,go)) for i in range(count)]
    await asyncio.sleep(.05);start=time.perf_counter();go.set()
    try:rows=await asyncio.gather(*tasks)
    finally:stop.set();await probe
    summary(label,rows,time.perf_counter()-start)
    RESULTS[-1]['health_probes']=probes;(OUT/'results.json').write_text(json.dumps(RESULTS,indent=2,ensure_ascii=False))

async def main():
    global WAV
    await phase('TTS WSS / 1',tts_one,1,True)
    if not PCM:raise RuntimeError('No fixture generated')
    WAV=subprocess.run(['ffmpeg','-v','error','-f','s16le','-ar','24000','-ac','1','-i','pipe:0','-ar','16000','-f','wav','pipe:1'],input=PCM,capture_output=True,check=True).stdout
    # ffmpeg pipe WAV length is unspecified; rewrite with an explicit size for clients.
    with wave.open(io.BytesIO(WAV),'rb') as f: raw=f.readframes(f.getnframes())
    out=io.BytesIO()
    with wave.open(out,'wb') as f:f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(raw)
    WAV=out.getvalue();(OUT/'fixture.wav').write_bytes(WAV)
    for n in (2,4):await phase(f'TTS WSS / {n}',tts_one,n)
    async def tts_http(i,go):return await tts_one(i,go,http=True)
    await phase('TTS HTTP streaming / 4',tts_http,4)
    for n in (1,2,4,8):await phase(f'STT HTTP / {n}',stt_http,n)
    for n in (1,4,8):await phase(f'STT WSS real-time / {n}',stt_ws,n)
    go=asyncio.Event();start=time.perf_counter()
    tasks=[asyncio.create_task(tts_one(i,go)) for i in range(4)]+[asyncio.create_task(stt_ws(i,go)) for i in range(4)]
    go.set();rows=await asyncio.gather(*tasks);wall=time.perf_counter()-start
    summary('Combined TTS WSS / 4',rows[:4],wall);summary('Combined STT WSS / 4',rows[4:],wall)

if __name__=='__main__':
    try:setup();asyncio.run(main())
    finally:
        for name,store in STORES.items():
            store.revoke_key(KEYS[name]['id'])
        print('Temporary audit keys revoked.',flush=True)
