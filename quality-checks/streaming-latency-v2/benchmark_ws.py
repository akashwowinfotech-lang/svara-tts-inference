import asyncio,json,sys,time,wave,os
from pathlib import Path
from websockets.asyncio.client import connect
root=Path(__file__).resolve().parents[2]
text=json.loads((root/'quality-checks/2026-10-15/single-pass/verification.json').read_text())['text']
label=sys.argv[1];uri=sys.argv[2] if len(sys.argv)>2 else 'ws://127.0.0.1:8088/v1/audio/speech/ws'
speed=float(sys.argv[3]) if len(sys.argv)>3 else 1.
voice=sys.argv[4] if len(sys.argv)>4 else 'mr_female'
if voice=='hi_male':text='नमस्कार! आज का दिन बहुत सुंदर है। हम आपके लिए स्वाभाविक आवाज़ में यह संदेश पढ़ रहे हैं। आपका स्वागत है और आपका दिन अच्छा हो।'
out=root/'quality-checks/streaming-latency-v2'
async def main():
    first=None;audio=[];packets=[];started=time.monotonic();playback_end=None;gaps=[]
    key=(root/'.secrets/owner-client.key').read_text().strip()
    connect_options = {'host':'127.0.0.1','port':443} if os.getenv('TTS_PROBE_LOCAL_TLS') else {}
    async with connect(uri,max_size=None,open_timeout=15,**connect_options) as ws:
        await ws.send(json.dumps({'model':'speech','voice':voice,'input':text,'api_key':key,'speed':speed}))
        async with asyncio.timeout(150):
            async for item in ws:
                if isinstance(item,bytes):
                    now=time.monotonic()-started;first=first or now;audio.append(item);packets.append([now,len(item)])
                    if playback_end is None:playback_end=now+.12
                    if now>playback_end:gaps.append(now-playback_end);playback_end=now+.12
                    playback_end+=len(item)/48000
                else:
                    event=json.loads(item)
                    if event.get('type')=='error':raise RuntimeError(event.get('message'))
                    if event.get('type')=='done':break
    pcm=b''.join(audio)
    with wave.open(str(out/(label+'.wav')),'wb') as f:
        f.setparams((1,2,24000,0,'NONE','not compressed'));f.writeframes(pcm)
    r={'label':label,'voice':voice,'speed':speed,'first_audio_seconds':first,'total_seconds':time.monotonic()-started,'audio_seconds':len(pcm)/48000,'playback_underruns':gaps,'packets':packets}
    (out/(label+'.json')).write_text(json.dumps(r,indent=2));print(json.dumps({k:v for k,v in r.items() if k!='packets'}))
asyncio.run(main())
