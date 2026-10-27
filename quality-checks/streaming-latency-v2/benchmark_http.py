import asyncio,json,sys,time
from pathlib import Path
import httpx
root=Path(__file__).resolve().parents[2];out=root/'quality-checks/streaming-latency-v2'
text=json.loads((root/'quality-checks/2026-10-15/single-pass/verification.json').read_text())['text']
async def main():
    label=sys.argv[1];fmt=sys.argv[2] if len(sys.argv)>2 else 'pcm';parts=[];packets=[];started=time.monotonic();first=None;total=0
    key=(root/'.secrets/owner-client.key').read_text().strip()
    async with httpx.AsyncClient(timeout=150) as client:
        async with client.stream('POST','http://127.0.0.1:8088/v1/audio/speech',headers={'Authorization':'Bearer '+key},json={'model':'speech','voice':'mr_female','input':text,'stream':True,'response_format':fmt}) as response:
            response.raise_for_status()
            headers=time.monotonic()-started
            async for part in response.aiter_bytes():
                now=time.monotonic()-started;parts.append(part);total+=len(part);packets.append([now,len(part)])
                if total>=512 and first is None:first=now
    (out/(label+'.'+fmt)).write_bytes(b''.join(parts))
    r={'label':label,'format':fmt,'headers_seconds':headers,'first_audio_payload_seconds':first,'total_seconds':time.monotonic()-started,'bytes':total,'packets':packets}
    (out/(label+'.json')).write_text(json.dumps(r,indent=2));print(json.dumps({k:v for k,v in r.items() if k!='packets'}))
asyncio.run(main())
