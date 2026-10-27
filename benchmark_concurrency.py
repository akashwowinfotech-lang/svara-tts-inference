"""Measure WebSocket speech latency under simultaneous, mixed-language requests."""

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from websockets.asyncio.client import connect


SAMPLES = (
    ("en_female", "Hello. Please speak this sentence clearly and naturally."),
    ("hi_male", "नमस्ते। कृपया यह वाक्य स्पष्ट और स्वाभाविक ढंग से बोलें।"),
    ("mr_female", "नमस्कार. कृपया हे वाक्य स्पष्ट आणि नैसर्गिक आवाजात बोला."),
    ("gu_male", "નમસ્તે. કૃપા કરીને આ વાક્ય સ્પષ્ટ અને સ્વાભાવિક રીતે બોલો."),
    ("kn_female", "ನಮಸ್ಕಾರ. ದಯವಿಟ್ಟು ಈ ವಾಕ್ಯವನ್ನು ಸ್ಪಷ್ಟವಾಗಿ ಮತ್ತು ಸಹಜವಾಗಿ ಮಾತನಾಡಿ."),
    ("te_male", "నమస్కారం. దయచేసి ఈ వాక్యాన్ని స్పష్టంగా మరియు సహజంగా చెప్పండి."),
)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


async def one_request(url, voice, text, api_key, ready, go):
    async with connect(url, open_timeout=20, close_timeout=10, max_size=None) as socket:
        ready.put_nowait(None)
        await go.wait()
        started = time.monotonic()
        await socket.send(json.dumps({
            "model": "speech", "voice": voice, "input": text,
            "response_format": "pcm", "steady_volume": True, "api_key": api_key,
        }))
        first_audio = None
        total_bytes = 0
        async with asyncio.timeout(120):
            async for message in socket:
                if isinstance(message, bytes):
                    first_audio = first_audio or time.monotonic()
                    total_bytes += len(message)
                else:
                    event = json.loads(message)
                    if event["type"] == "error":
                        raise RuntimeError(event["message"])
                    if event["type"] == "done":
                        break
        if first_audio is None or total_bytes == 0:
            raise RuntimeError("No audio received")
        return first_audio - started, time.monotonic() - started, total_bytes / 48000


async def measure(url, count, api_key):
    ready = asyncio.Queue()
    go = asyncio.Event()
    tasks = [asyncio.create_task(one_request(url, *SAMPLES[i % len(SAMPLES)], api_key, ready, go))
             for i in range(count)]
    try:
        async with asyncio.timeout(30):
            for _ in tasks:
                await ready.get()
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    wall_start = time.monotonic()
    go.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    wall = time.monotonic() - wall_start
    good = [result for result in results if not isinstance(result, BaseException)]
    errors = [str(result) for result in results if isinstance(result, BaseException)]
    if good:
        first = [result[0] for result in good]
        total = [result[1] for result in good]
        audio_seconds = sum(result[2] for result in good)
        print(f"{count:>2} concurrent: {len(good)}/{count} OK | "
              f"first audio p50/p95 {statistics.median(first):.2f}/{percentile(first, .95):.2f}s | "
              f"complete p95 {percentile(total, .95):.2f}s | "
              f"wall {wall:.2f}s | generated {audio_seconds / wall:.2f} audio-s/s", flush=True)
    else:
        print(f"{count:>2} concurrent: 0/{count} OK | wall {wall:.2f}s", flush=True)
    if errors:
        print(f"  errors: {errors[:3]}", flush=True)
    return not errors


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8088/v1/audio/speech/ws")
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 2, 4, 8, 16])
    parser.add_argument("--key-file", default=str(Path(__file__).parent / ".secrets/owner-client.key"))
    args = parser.parse_args()
    api_key = Path(args.key_file).read_text().strip()
    for count in args.levels:
        if not await measure(args.url, count, api_key):
            break


if __name__ == "__main__":
    asyncio.run(main())
