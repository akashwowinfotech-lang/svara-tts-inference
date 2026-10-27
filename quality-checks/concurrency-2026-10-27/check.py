"""Bounded live HTTP/WS concurrency check. No credentials written to reports."""
import asyncio
import hashlib
import json
import statistics
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.access import AccessStore

OUT = Path(__file__).resolve().parent
SAMPLES = [
    ('en_female', 'Hello. Welcome to our service. We are checking clear and natural speech.'),
    ('hi_male', 'नमस्ते। हमारी सेवा में आपका स्वागत है। हम स्पष्ट और स्वाभाविक आवाज़ की जाँच कर रहे हैं।'),
    ('mr_female', 'नमस्कार. आमच्या सेवेत आपले स्वागत आहे. आम्ही स्पष्ट आणि नैसर्गिक आवाजाची चाचणी करत आहोत.'),
    ('gu_male', 'નમસ્તે. અમારી સેવામાં આપનું સ્વાગત છે. અમે સ્પષ્ટ અને સ્વાભાવિક અવાજની તપાસ કરી રહ્યા છીએ.'),
    ('kn_female', 'ನಮಸ್ಕಾರ. ನಮ್ಮ ಸೇವೆಗೆ ಸ್ವಾಗತ. ನಾವು ಸ್ಪಷ್ಟ ಮತ್ತು ಸಹಜ ಧ್ವನಿಯನ್ನು ಪರೀಕ್ಷಿಸುತ್ತಿದ್ದೇವೆ.'),
    ('te_male', 'నమస్కారం. మా సేవకు స్వాగతం. మేము స్పష్టమైన మరియు సహజమైన స్వరాన్ని పరీక్షిస్తున్నాము.'),
]

async def request(client, key, transport, sample, go, label):
    voice, text = sample
    await go.wait()
    start = time.monotonic()
    packets = []
    pcm = bytearray()
    done = False
    status = None
    try:
        payload = {'model': 'speech', 'voice': voice, 'input': text,
                   'stream': True, 'response_format': 'pcm', 'steady_volume': True}
        async with asyncio.timeout(100):
            if transport == 'ws':
                async with connect('ws://127.0.0.1:8088/v1/audio/speech/ws', max_size=None) as ws:
                    await ws.send(json.dumps(dict(payload, api_key=key)))
                    async for message in ws:
                        if isinstance(message, bytes):
                            packets.append([time.monotonic()-start, len(message)])
                            pcm.extend(message)
                        else:
                            event = json.loads(message)
                            if event['type'] == 'error':
                                raise RuntimeError(event['message'])
                            if event['type'] == 'done':
                                done = True
                                break
            else:
                async with client.stream('POST', 'http://127.0.0.1:8088/v1/audio/speech',
                                         headers={'Authorization': 'Bearer '+key}, json=payload) as response:
                    status = response.status_code
                    if status != 200:
                        raise RuntimeError((await response.aread()).decode())
                    async for part in response.aiter_bytes():
                        packets.append([time.monotonic()-start, len(part)])
                        pcm.extend(part)
                    done = True
        if not done or not pcm or len(pcm) % 2:
            raise RuntimeError('Missing completion, empty audio, or invalid PCM length')
        samples = np.frombuffer(pcm, '<i2').astype(float)/32768
        if np.max(np.abs(samples)) == 0:
            raise RuntimeError('Silent audio')
        with wave.open(str(OUT/(label+'-'+voice+'-'+transport+'.wav')), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(pcm)
        # Underruns after first audio with a 250 ms client startup buffer.
        playback_end = packets[0][0]+.25
        stalls = []
        for arrival, size in packets:
            if arrival > playback_end:
                stalls.append(arrival-playback_end)
                playback_end = arrival
            playback_end += size/48000
        return {'transport': transport, 'voice': voice, 'ok': True, 'http_status': status,
                'first_audio_s': packets[0][0], 'total_s': time.monotonic()-start,
                'audio_s': len(pcm)/48000, 'packets': packets,
                'underruns_250ms_buffer_s': stalls,
                'peak': float(np.max(np.abs(samples))),
                'rms': float(np.sqrt(np.mean(samples*samples))),
                'sha256': hashlib.sha256(pcm).hexdigest()}
    except Exception as error:
        return {'transport': transport, 'voice': voice, 'ok': False, 'http_status': status,
                'total_s': time.monotonic()-start, 'error': str(error)}

async def measure(client, key, transports, label, offset=0):
    go = asyncio.Event()
    tasks = [asyncio.create_task(request(client, key, transport, SAMPLES[(i+offset)%6], go, label))
             for i, transport in enumerate(transports)]
    health = []
    stop = asyncio.Event()
    async def health_monitor():
        while not stop.is_set():
            started = time.monotonic()
            try:
                response = await client.get('http://127.0.0.1:8088/health', timeout=5)
                health.append({'seconds': time.monotonic()-started, 'status': response.status_code})
            except Exception as error:
                health.append({'seconds': time.monotonic()-started, 'error': str(error)})
            try:
                await asyncio.wait_for(stop.wait(), .25)
            except asyncio.TimeoutError:
                pass
    monitor = asyncio.create_task(health_monitor())
    started = time.monotonic()
    go.set()
    try:
        results = await asyncio.gather(*tasks)
    finally:
        stop.set()
        await monitor
    wall = time.monotonic()-started
    good = [result for result in results if result['ok']]
    summary = {'label': label, 'connections': len(tasks), 'ok': len(good),
               'wall_s': wall, 'requests': results, 'health': health}
    if good:
        first = sorted(result['first_audio_s'] for result in good)
        summary.update(first_audio_p50_s=statistics.median(first), first_audio_max_s=max(first),
                       audio_seconds_per_wall_second=sum(r['audio_s'] for r in good)/wall,
                       max_health_s=max(r['seconds'] for r in health),
                       underrun_requests=sum(bool(r['underruns_250ms_buffer_s']) for r in good))
        # Audio arrivals before the earliest completion demonstrate overlap.
        summary['streams_started_before_first_completion'] = sum(
            r['first_audio_s'] < min(x['total_s'] for x in good) for r in good)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('requests','health')}) , flush=True)
    (OUT/(label+'.json')).write_text(json.dumps(summary, indent=2))
    return summary

async def main():
    # This secret is solely a required constructor value; never authenticates admin access.
    store = AccessStore(str(ROOT/'.data/access.sqlite3'), 'local-benchmark-unused-admin-value-000000')
    issued = store.create_key('Local concurrency audit 2026-10-27', ['127.0.0.1/32'], 1, 16, 120)
    all_results = []
    try:
        async with httpx.AsyncClient(timeout=100, trust_env=False) as client:
            for label, transports, offset in [
                ('ws-1', ['ws'], 0), ('ws-2', ['ws']*2, 0),
                ('ws-4', ['ws']*4, 0), ('http-1', ['http'], 0),
                ('http-4', ['http']*4, 2), ('mixed-4', ['http','ws','http','ws'], 0),
                ('overload-6', ['http','ws']*3, 0),
                ('recovery-1', ['ws'], 0),
            ]:
                all_results.append(await measure(client, issued['key'], transports, label, offset))
    finally:
        store.revoke_key(issued['id'])
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'scope': 'local origin, preset PCM streaming, speed 1x', 'results': all_results}
    (OUT/'report.json').write_text(json.dumps(report, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
