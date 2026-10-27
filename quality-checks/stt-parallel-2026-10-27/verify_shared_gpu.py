import asyncio,importlib.util,json
from pathlib import Path
ROOT=Path('/home/wowbharat/WOWBHARAT/TTS/upstream-svara')
spec=importlib.util.spec_from_file_location('audit',ROOT/'quality-checks/concurrency-2026-10-27/benchmark.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
audit.WAV=(ROOT/'quality-checks/concurrency-2026-10-27/fixture.wav').read_bytes()
async def main():
 go=asyncio.Event()
 async def delayed_tts():
  await go.wait();await asyncio.sleep(4);return await audit.tts_one(0,go)
 tasks=[asyncio.create_task(audit.stt_ws(i,go)) for i in range(2)]+[asyncio.create_task(delayed_tts())]
 go.set();rows=await asyncio.gather(*tasks)
 (ROOT/'quality-checks/stt-parallel-2026-10-27/shared-gpu.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
 print(json.dumps(rows,ensure_ascii=False),flush=True)
 assert all(r['ok'] for r in rows)
try:audit.setup();asyncio.run(main())
finally:
 for name,store in audit.STORES.items():store.revoke_key(audit.KEYS[name]['id'])
