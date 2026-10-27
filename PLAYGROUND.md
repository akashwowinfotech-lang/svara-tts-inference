# Speech browser playground

This checkout adds a single static page at `http://localhost:8080/`, a
WebSocket streaming endpoint, and keeps the existing HTTP API. The page
lets you test preset male and female voices for English, Hindi, Marathi,
Gujarati, Kannada, and Telugu. Expressive tags such as `<laugh>` can be typed
or inserted with the buttons. Their effect depends on the released model and
language; an explicit breath tag is not documented for this checkpoint.

The **Stable volume** control is on by default for both HTTP and WebSocket
speech. It gently adjusts the level across generated segments and limits peaks.
Set `steady_volume` to `false` in an API request to hear the original dynamics.
This can reduce volume jumps, but it cannot prevent changes in the model's
voice character or pronunciation between separately generated segments.

## Run with an NVIDIA GPU

The upstream server embeds vLLM and loads `kenpath/svara-tts-v1` at startup.
Use an NVIDIA machine with Docker, the NVIDIA Container Toolkit, and at least
24 GB of GPU memory recommended by the upstream project. The first start
downloads model and codec weights and can take several minutes.

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:8080/>. `GET /health` reports server readiness and
authenticated `GET /v1/voices` lists available voices. The public API uses the alias
`speech` and does not expose the internal model identifier in its page,
metadata, or health response.

### Native setup used on this RTX 5090

Docker is not installed on this host, so the working local setup uses a
project-only virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt soundfile numpy
bash run-local.sh
```

Open <http://127.0.0.1:8088/>. The script allocates 55% of GPU memory to vLLM,
uses eager execution, and decodes SNAC on the CPU. A development Python header
available on this host is added to the compiler path. Adjust
`VLLM_GPU_MEMORY_UTILIZATION` if other workloads use the GPU. On first start, the
script creates private admin and local client keys. See [PRODUCTION.md](PRODUCTION.md)
for the dashboard, access controls, and later domain setup.

To check the live server:

```bash
.venv/bin/python smoke_test.py
```

On 25 September 2026 this RTX 5090 returned all 12 requested presets. A warm
English WebSocket smoke test produced first audio in 0.55 seconds and 1.96
seconds of PCM in 1.26 seconds overall. The first cold HTTP request took 31
seconds. The browser page also completed both WebSocket and HTTP streaming
requests. A mixed-language load test completed 16/16 simultaneous requests,
but first audio rose to 9.12 seconds at that load. Keep live service near
3–4 simultaneous speech requests on this setup.

## HTTP streaming

```bash
curl -N http://localhost:8080/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(cat .secrets/owner-client.key)" \
  -d '{"model":"speech","input":"नमस्ते! आप कैसे हैं?","voice":"hi_female","response_format":"pcm","stream":true}' \
  --output speech.pcm
```

HTTP returns raw signed 16-bit little-endian PCM at 24 kHz, mono. Set
`response_format` to `wav`, `mp3`, `opus`, or `aac` to use the upstream format
converter. The browser page uses PCM for low-overhead progressive playback.

## WebSocket streaming

Connect to `ws://localhost:8080/v1/audio/speech/ws` (use `wss://` behind
HTTPS). Send one JSON request per connection:

```json
{"model":"speech","input":"Hello there! <happy>","voice":"en_female","api_key":"CLIENT_KEY"}
```

The server sends a JSON `start` message containing the audio format and
sample rate, then binary PCM chunks, then a JSON `done` message. Errors are
JSON `error` messages. Closing the socket stops the active request. Create a
new connection for each utterance. The server uses the existing async
generator, so audio is sent as it is synthesized. The model and GPU determine
actual time to first audio and throughput under concurrent connections.

The WebSocket route accepts preset voices only. The HTTP
endpoint also accepts base64 `reference_audio` for voice cloning.

## Limits of verification

GPU inference, HTTP audio generation, WebSocket streaming, browser playback,
and short-utterance concurrency were verified on this host. Naturalness,
laughter and breath quality, and sustained production traffic have not yet
been measured by native listeners or an extended load test.
