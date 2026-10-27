# Speech API — developer integration guide

This guide covers the public speech endpoints. Use the API key issued to your application. Keep it on your server, send it only over HTTPS/WSS, and never place it in a URL or a public source repository.

**Base URL:** `https://api.tts.ganas.ai`  
**WebSocket URL:** `wss://api.tts.ganas.ai/v1/audio/speech/ws`  
**API version:** `v1`

## Quick start

Set your client key in a local environment variable:

```bash
export SPEECH_API_KEY='YOUR_CLIENT_KEY'
```

List available voices:

```bash
curl --fail-with-body 'https://api.tts.ganas.ai/v1/voices' \
  -H "Authorization: Bearer $SPEECH_API_KEY"
```

Generate an MP3 file:

```bash
curl --fail-with-body 'https://api.tts.ganas.ai/v1/audio/speech' \
  -H "Authorization: Bearer $SPEECH_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"model":"speech","input":"नमस्ते! आपका स्वागत है।","voice":"hi_female","response_format":"mp3"}' \
  --output speech.mp3
```

## Endpoints

| Method | Path | Purpose | Authentication |
| --- | --- | --- | --- |
| `GET` | `/health` | Readiness check; returns `{"status":"healthy"}` | None |
| `GET` | `/v1/voices` | List voice IDs and metadata | `Authorization: Bearer CLIENT_KEY` |
| `POST` | `/v1/audio/speech` | Generate audio as one response or an HTTP stream | `Authorization: Bearer CLIENT_KEY` |
| WebSocket | `/v1/audio/speech/ws` | Stream raw PCM audio frames | `api_key` in the first JSON message |

For HTTP POST requests, send `Content-Type: application/json`. An API key may also be restricted by source IP, expiry, requests per minute, and simultaneous speech requests. A WebSocket connection accepts **one utterance**; open a new connection for each request.

When speech capacity is busy, requests may wait in a shared queue for up to 30 seconds before generation starts. Scheduling uses the priority assigned to the client key; it cannot be overridden in a speech request. Waiting time is part of time to first audio. Active streams finish normally. Queue capacity and per-key waiting limits are bounded; a full queue or wait timeout returns HTTP `429`, or a WebSocket error frame. Set your read timeout to cover both queue wait and generation, and retry busy responses with backoff and jitter.

## Voices

`GET /v1/voices` returns a `voices` array. Each item has `voice_id`, `name`, `languages`, `language_code`, and optional `gender` and `description` fields. Use `voice_id` in speech requests. Always fetch this endpoint if you need the complete current catalog.

```json
{
  "voices": [
    {
      "voice_id": "hi_female",
      "name": "Hindi (Female)",
      "languages": ["hi"],
      "language_code": "hi",
      "gender": "female"
    }
  ]
}
```

The core male/female IDs for the six main languages are:

| Language | Male | Female |
| --- | --- | --- |
| English (Indian) | `en_male` | `en_female` |
| Hindi | `hi_male` | `hi_female` |
| Marathi | `mr_male` | `mr_female` |
| Gujarati | `gu_male` | `gu_female` |
| Kannada | `kn_male` | `kn_female` |
| Telugu | `te_male` | `te_female` |

## HTTP speech generation

`POST /v1/audio/speech` returns audio bytes, not JSON. `stream: false` waits for the complete result; `stream: true` delivers audio while the model is still generating the phrase. Decoding retains surrounding codec context, every opening/ending sample, and one voice-model generation per phrase. The response media type depends on `response_format`.

The user-approved Marathi female preset now keeps fitting paragraphs in one
generation by default. It permits up to 1000 characters and an estimated 160
spoken units in the 4096-token context, with an output budget up to the available
context. Other presets retain their 200-character / 60-unit / 2048-output-token
defaults: an expanded Hindi male test repeated its ending, so a universal rollout
has not been accepted. Explicit `chunk_size` and `max_tokens` options remain
available across all 38 native presets / 19 languages for comparisons. These
options do not certify pronunciation or prevent every repetition. Longer text
still splits and can change voice between generations. Larger generations take
longer before their first audio.

Node.js 18+ example:

```javascript
import { writeFile } from 'node:fs/promises';

const response = await fetch('https://api.tts.ganas.ai/v1/audio/speech', {
  method: 'POST',
  headers: {
    Authorization: `Bearer ${process.env.SPEECH_API_KEY}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    model: 'speech',
    input: 'Hello, welcome to our service.',
    voice: 'en_female',
    response_format: 'mp3',
  }),
});
if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
await writeFile('speech.mp3', Buffer.from(await response.arrayBuffer()));
```

### Request fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `input` | string | Required | Text to speak, 1–5,000 characters. Unicode text is supported. |
| `voice` | string | Required | A `voice_id` from `/v1/voices`. Preset IDs are recommended. |
| `model` | string | `speech` | Public compatibility alias. Use `speech` if your SDK requires this field. |
| `response_format` | string | `mp3` | `mp3`, `wav`, `opus`, `aac`, or `pcm`. |
| `stream` | boolean | `false` | If true, the HTTP response is sent progressively. |
| `steady_volume` | boolean | `true` | Smooths level changes between generated segments. |
| `speed` | number | `1.0` | Pitch-preserving speed multiplier, 0.5–2.0. At 1.0 the decoded voice and native cadence bypass time stretching. The same multiplier applies to HTTP and WebSocket; different language presets have their own natural speaking rhythms. |
| `temperature` | number | Server default | Sampling control, 0.0–2.0. |
| `top_p` | number | Server default | Sampling control, 0.0–1.0. |
| `top_k` | integer | Server default | Sampling control, minimum −1. |
| `repetition_penalty` | number | Server default | Sampling control, 1.0–2.0. |
| `max_tokens` | integer | Server default | Generation cap, 1–4,096. |
| `chunk_size` | integer | Server default | Text segmentation size, 50–1,000 characters. |
| `buffer_ms` | integer | `600` | PCM packet size, 0–5,000 ms (minimum 100 ms). Both WebSocket and HTTP with `stream:true` decode progressively; larger packets can delay first audio. |
| `reference_audio` | base64 string | Omitted | Optional reference audio for HTTP voice cloning. WAV, MP3, FLAC, and OGG input can be decoded. |
| `reference_transcript` | string | Omitted | Transcript of the reference audio; can improve cloning. |

Use only the controls your application needs. Sampling and segmentation settings can affect voice consistency, latency, and pronunciation. The request body is limited to **10 MB** at the public proxy, including base64 reference audio.

### Audio formats

| `response_format` | Response `Content-Type` | Audio data |
| --- | --- | --- |
| `mp3` | `audio/mpeg` | Encoded MP3; convenient for files and broad player support. |
| `wav` | `audio/wav` | WAV; convenient for downloads. |
| `opus` | `audio/ogg` | Opus in an Ogg container. |
| `aac` | `audio/aac` | AAC ADTS stream. |
| `pcm` | `audio/pcm` | Signed 16-bit little-endian PCM, mono, 24,000 samples/second; no WAV header. |

For the lowest-overhead progressive playback, use `"stream":true` with `"response_format":"pcm"`. The streaming HTTP response also includes `X-Sample-Rate: 24000` and `X-Channels: 1`. PCM audio carries **48,000 bytes per second**. If you save it to a file, use a `.pcm` extension or wrap it in a WAV header before playback.

MP3, AAC, Opus and WAV streaming also encode progressively, retaining their existing codec quality settings. Format headers alone are not playable audio; clients should begin playback once audio payload is available. Avoid collecting the entire HTTP response into a Blob or calling `arrayBuffer()` before starting playback, since that waits for the full recording. Faster playback uses the same startup reserve and fixed phrase gain as WebSocket.

```bash
curl --no-buffer --fail-with-body 'https://api.tts.ganas.ai/v1/audio/speech' \
  -H "Authorization: Bearer $SPEECH_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"model":"speech","input":"Hello, welcome to our service.","voice":"en_female","response_format":"pcm","stream":true}' \
  --output speech.pcm

# Convert raw PCM to a WAV file if needed:
ffmpeg -f s16le -ar 24000 -ac 1 -i speech.pcm speech.wav
```

### Optional reference voice (HTTP only)

Send `reference_audio` as a base64 string and, when available, `reference_transcript`. Keep the entire JSON body below 10 MB. WebSocket requests do not support reference audio.

```python
import base64
import os
import requests

with open("reference.wav", "rb") as audio_file:
    reference_audio = base64.b64encode(audio_file.read()).decode("ascii")

response = requests.post(
    "https://api.tts.ganas.ai/v1/audio/speech",
    headers={"Authorization": f"Bearer {os.environ['SPEECH_API_KEY']}"},
    json={
        "model": "speech",
        "input": "Hello, this is a short sample.",
        "voice": "en_female",
        "response_format": "wav",
        "reference_audio": reference_audio,
        "reference_transcript": "Transcript of the reference clip.",
    },
    timeout=120,
)
response.raise_for_status()
with open("cloned-speech.wav", "wb") as output:
    output.write(response.content)
```

## WebSocket streaming

Connect to `wss://api.tts.ganas.ai/v1/audio/speech/ws`. After the connection opens, send exactly one JSON object containing `api_key`, `input`, and a **preset** `voice_id`. `model`, `steady_volume`, `chunk_size`, `buffer_ms`, and generation controls may also be supplied. The WebSocket always returns PCM, so `response_format` is not used to choose its output format.

Audio arrives while the model is still generating the phrase. The server decodes bounded codec windows with surrounding context; network packets do not restart the voice model or split words. With `steady_volume=true`, gain is estimated from initial speech and held fixed within the phrase, with smooth peak protection. Speed changes use one continuous pitch-preserving filter per phrase. Faster speeds include a startup reserve (up to 2.4 seconds of output audio at 2x) to reduce playback gaps. Begin playing binary frames as they arrive using a small playback queue; waiting for `done` buffers the entire utterance. Model queues, shared GPU load and generation speed still affect latency.

Client's first text frame:

```json
{"api_key":"YOUR_CLIENT_KEY","model":"speech","input":"नमस्ते! आप कैसे हैं?","voice":"hi_female","steady_volume":true}
```

Server frame sequence:

1. Text frame: `{"type":"start","format":"pcm_s16le","sample_rate":24000,"channels":1,"voice":"hi_female"}`.
2. One or more **binary** frames containing consecutive raw signed 16-bit little-endian PCM samples. Append them in arrival order; frame boundaries do not indicate words or sentences.
3. Text frame: `{"type":"done"}`, followed by normal socket closure.

On failure, the server sends a text frame such as `{"type":"error","message":"Invalid speech request"}` and closes the socket. Authentication or request-policy errors normally close with code `1008`; server failures use `1011`. Closing the socket yourself cancels the active generation.

Python example (`pip install websockets`):

```python
import asyncio
import json
import os
from websockets.asyncio.client import connect

async def main():
    async with connect("wss://api.tts.ganas.ai/v1/audio/speech/ws") as socket:
        await socket.send(json.dumps({
            "api_key": os.environ["SPEECH_API_KEY"],
            "model": "speech",
            "input": "Hello from a WebSocket client.",
            "voice": "en_female",
            "steady_volume": True,
        }))
        with open("speech.pcm", "wb") as output:
            async for frame in socket:
                if isinstance(frame, bytes):
                    output.write(frame)
                    continue
                event = json.loads(frame)
                if event["type"] == "start":
                    print("Audio:", event["format"], event["sample_rate"], "Hz")
                elif event["type"] == "done":
                    break
                elif event["type"] == "error":
                    raise RuntimeError(event["message"])

asyncio.run(main())
```

For browser playback, create an `AudioBuffer` at 24 kHz and convert each 16-bit sample to a float by dividing by 32768. Queue at least 600 ms before starting, then schedule each buffer exactly at the previous buffer's end. Flush a shorter queue when `done` arrives. Network frames are transport pieces, not individual words: do not play them as separate audio elements, and send complete clauses or sentences in each synthesis request. A same-origin browser client works with this endpoint; a different website origin needs explicit cross-origin approval. Do not put a long-lived client key in public JavaScript.

## Errors and retries

HTTP failures return JSON with a `detail` field. Validation failures may contain an array of field errors rather than a single string.

| HTTP status | Meaning | Client action |
| --- | --- | --- |
| `401` | Missing, invalid, expired, or revoked client key | Check the key; request a replacement if needed. |
| `403` | Source IP is not permitted | Use an approved IP or request an IP-rule change. |
| `413` | Request body exceeds the public proxy limit | Shorten the text or reference audio. |
| `422` | Request JSON failed validation | Correct required fields or parameter ranges. |
| `429` | Rate limit reached, waiting queue full, or queue wait timed out | Retry with exponential backoff and jitter. |
| `5xx` | Temporary service or generation failure | Retry with backoff; report persistent failures. |

Speech requests can take longer when the text is long or other requests are active. Set client read timeouts accordingly. Do not assume fixed audio-chunk sizes or a fixed first-audio latency. Optional expressive tags such as `<happy>`, `<sad>`, `<laugh>`, `<giggle>`, and `<whispering>` can be included in text, but their effect varies by voice and language.
