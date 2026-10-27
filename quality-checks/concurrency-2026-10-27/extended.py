"""Verify two-stream playback across remaining languages and public WSS proxy."""
import asyncio
import json
from pathlib import Path
import httpx
import check
from websockets.asyncio.client import connect as tls_connect

async def main():
    store = check.AccessStore(str(check.ROOT/'.data/access.sqlite3'), 'local-benchmark-unused-admin-value-000000')
    issued = store.create_key('Local two-stream and TLS audit 2026-10-27', ['127.0.0.1/32'], 1, 2, 60)
    reports = []
    try:
        async with httpx.AsyncClient(timeout=100, trust_env=False) as client:
            for label, transports, offset in [
                ('mixed-2-marathi-gujarati', ['http', 'ws'], 2),
                ('mixed-2-kannada-telugu', ['http', 'ws'], 4),
                ('per-key-overload-3', ['http', 'ws', 'http'], 0),
            ]:
                reports.append(await check.measure(client, issued['key'], transports, label, offset))
            check.SAMPLES[0] = (check.SAMPLES[0][0], check.SAMPLES[0][1]*4)
            check.SAMPLES[1] = (check.SAMPLES[1][0], check.SAMPLES[1][1]*4)
            reports.append(await check.measure(client, issued['key'], ['http', 'ws'], 'long-mixed-2'))
            # Route through Nginx TLS on loopback, keeping the real hostname for SNI and certificate verification.
            check.connect = lambda _url, **options: tls_connect(
                'wss://api.tts.ganas.ai/v1/audio/speech/ws', host='127.0.0.1', port=443, **options)
            check.SAMPLES[0] = ('en_female', 'Hello. This is a secure streaming connection test.')
            check.SAMPLES[1] = ('hi_male', 'नमस्ते। यह सुरक्षित स्ट्रीमिंग कनेक्शन की जाँच है।')
            reports.append(await check.measure(client, issued['key'], ['ws', 'ws'], 'tls-proxy-wss-2'))
    finally:
        store.revoke_key(issued['id'])
    (Path(__file__).parent/'extended-report.json').write_text(json.dumps(reports, indent=2))

asyncio.run(main())
