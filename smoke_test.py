"""Quick live check of HTTP health, voices, and WebSocket PCM stream."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect


async def check(base_url: str, api_key: str) -> None:
    with urlopen(f"{base_url}/health", timeout=10) as response:
        health = json.load(response)
    assert health["status"] == "healthy", health

    with urlopen(Request(f"{base_url}/v1/voices", headers={"Authorization": f"Bearer {api_key}"}), timeout=10) as response:
        voices = json.load(response)["voices"]
    wanted = {f"{language}_{gender}" for language in ("en", "hi", "mr", "gu", "kn", "te") for gender in ("male", "female")}
    available = {voice["voice_id"] for voice in voices}
    assert wanted <= available, wanted - available

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/v1/audio/speech/ws"
    started = time.monotonic()
    first_audio = None
    byte_count = 0
    async with connect(ws_url, open_timeout=10, close_timeout=10) as socket:
        await socket.send(json.dumps({
            "model": "speech", "voice": "en_female", "api_key": api_key,
            "input": "Hello. This is a test.",
        }))
        async for message in socket:
            if isinstance(message, bytes):
                first_audio = first_audio or time.monotonic()
                byte_count += len(message)
            else:
                event = json.loads(message)
                if event["type"] == "error":
                    raise RuntimeError(event["message"])
                if event["type"] == "done":
                    break
                assert event == {
                    "type": "start", "format": "pcm_s16le", "sample_rate": 24000,
                    "channels": 1, "voice": "en_female",
                }, event

    assert first_audio is not None and byte_count > 0, "No PCM audio received"
    print(f"HTTP health: OK; required voices: {len(wanted)}/{len(wanted)}")
    print(f"WebSocket first audio: {first_audio - started:.2f}s")
    print(f"WebSocket total: {time.monotonic() - started:.2f}s; PCM: {byte_count / 48000:.2f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    parser.add_argument("--key-file", default=str(Path(__file__).parent / ".secrets/owner-client.key"))
    args = parser.parse_args()
    asyncio.run(check(args.url.rstrip("/"), Path(args.key_file).read_text().strip()))
