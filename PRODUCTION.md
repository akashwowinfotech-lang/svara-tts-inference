# Speech API operations

The local server binds to `127.0.0.1:8088`. On this host,
`speech-api.service` is enabled for boot and restarts the model if it fails.
Nginx and the Certbot renewal timer are also enabled. Set or rotate the
dashboard login with `.venv/bin/python set-admin-login.py --username YOUR_NAME`,
then restart `speech-api.service`. The login setup and first start create
these private files:

- `.secrets/admin-login.json` — salted password hash for the dashboard login.
- `.secrets/admin.key` — internal bootstrap secret; it is not a dashboard login.
- `.secrets/owner-client.key` — local-only key for the speech playground.
- `.data/access.sqlite3` — client key hashes, IP rules, and request metadata.

The keys and database are Git-ignored. Back up `.data` and `.secrets` securely;
losing the database revokes all client keys. Do not put either folder into a
public web root. Dashboard sessions last eight hours and use an HttpOnly cookie.

Open `http://127.0.0.1:8088/admin` locally, or use
`https://api.tts.ganas.ai/admin` after TLS is active. Sign in with the configured
username and password, then create a separate client key for each external user
or application. A new key appears only once. You can set an
expiry, per-key IP/CIDR allowlist, concurrent request limit, and requests per
minute, then edit the limits or revoke the key. Revoked keys can be deleted from
the panel; request history remains until the retention period ends. The global client IP allowlist
is empty by default, meaning any IP with a valid key may call the speech API.
If you set it, both the global and per-key lists must allow the client IP.
The admin login accepts a valid username and password from any IP over HTTPS.
Sessions remain bound to the login IP, and repeated failures are rate limited.

The dashboard filters usage and request history by preset period or a custom
date range in the viewer's local timezone, plus status, transport, endpoint,
client key, source IP, and voice. The client-key list has separate filters for
creation, expiry, or revocation dates. The layout adapts to phones and tablets.
It reports status, client, source IP, transport, voice,
audio bytes, generated audio minutes, duration, and time to first audio. Exact
minute totals start when audio metering is deployed; older logs have no exact
audio duration. It never stores submitted text, voice
references, or generated audio. Reports expire after 30 days by default;
configure `ACCESS_LOG_RETENTION_DAYS` if needed. `MAX_CONCURRENT_SPEECH=4`
is the shared HTTP/WebSocket active-stream limit. Two concurrent streams were
smooth in the 27 October audit; four needed additional client playback buffering.

## Client priority and waiting requests

Create or edit a key's Priority as High (1), Normal (2), or Low (3), then Save.
Existing keys default to Normal. Changes apply to new requests. Priority chooses
the next eligible waiting request; it does not interrupt active audio or increase
GPU capacity. Equal priorities use arrival order. Every 10 seconds of waiting
improves effective priority by one level, to a minimum of zero, so older requests
can move ahead of newer high-priority traffic. A key already at its concurrency
limit does not block eligible requests from another key.

The shared queue holds up to 64 waiting requests, at most eight per key, with a
30-second wait timeout. Rate limits count admitted and queued requests. Queue
overflow, wait timeout and rate limits return HTTP 429 or a WebSocket error.
Queued clients are removed on disconnect; access is rechecked before generation
so a revoked or expired key cannot start queued work. The dashboard reports
active and waiting totals and waiting counts by priority. The queue is in memory
in the single API worker; waiting requests do not survive a restart.

Settings: `MAX_QUEUED_SPEECH=64`, `MAX_QUEUED_SPEECH_PER_KEY=8`,
`SPEECH_QUEUE_TIMEOUT=30`, `SPEECH_PRIORITY_AGING_SECONDS=10`.
Set `MAX_QUEUED_SPEECH=0` for immediate busy responses. Keep client read timeouts
longer than queue wait plus generation time. Queue time is included in the
reported time to first audio and request duration.

## HTTP and WebSocket authentication

Speech preparation retains zero-valued SNAC codes and surrounding codec context,
and applies pitch-preserving tempo changes only when `speed` differs from 1.0.
Normal speed preserves the decoded waveform and native cadence; automatic
text-derived duration normalization and silence compression were removed after
they changed perceived voice character. Streaming sends audio during generation;
non-streaming requests wait for the complete result. Browser clients should
queue 600 ms and play PCM buffers contiguously. The default sampling
temperature remains the original 0.75 without an imposed seed.

Send `Authorization: Bearer CLIENT_KEY` on HTTP requests to `/v1/voices` and
`/v1/audio/speech`. For WebSocket, connect to `/v1/audio/speech/ws` and put
`"api_key":"CLIENT_KEY"` in the first JSON message. Do not put keys in URL
query strings. The browser playground accepts a client key in its password
field and sends it by the same methods.

Use HTTPS/WSS for any remote client. The server's `/health` endpoint stays
unauthenticated for health checks. API docs are disabled by default. Keep the
server on one Uvicorn worker because the embedded model and in-memory request
limiter are sized for one GPU process.

## Public access for api.tts.ganas.ai

DNS points to `103.183.80.247`. This server is behind the router at
`192.168.1.1` and has the static LAN address `192.168.1.35`. The router
forwards **TCP 80 → 192.168.1.35:80** and **TCP 443 → 192.168.1.35:443**.
Nginx proxies the hostname to the local speech process and redirects HTTP to
HTTPS. The Let's Encrypt certificate was issued on 2026-09-30 and its renewal
dry run passed. The public `/health`, `/admin`, authenticated `/v1/voices`, and
WebSocket speech endpoints were checked on 2026-09-30.

If this domain needs to be reinstalled, run:

```bash
cd /home/wowbharat/WOWBHARAT/TTS/upstream-svara
sudo bash deploy/install-domain.sh
```

The installer requests or reuses the Let's Encrypt certificate, enables
renewal, and checks `https://api.tts.ganas.ai/health`. Create an external client
key from `/admin` for each user or application and set that client's IP rule if
known. Do not share client keys over plain HTTP.

The proxy must overwrite `X-Real-IP` and be the only public route to port
8088. `TRUSTED_PROXY_IPS` controls whether the application trusts that header.
The local runner disables Uvicorn's automatic proxy-header handling so direct
clients cannot spoof the IP used by allowlists. For a web application hosted
on another origin, set `CORS_ORIGINS` to that exact HTTPS origin; same-origin
playground use needs no CORS setting.

Rotate the dashboard password with `.venv/bin/python set-admin-login.py --username YOUR_NAME`,
then restart the service. This revokes dashboard sessions. Revoke and reissue
any client key that may have leaked. For a new Docker volume, run
`docker compose run --rm --no-deps svara-tts-api python3 set-admin-login.py --username YOUR_NAME`
before starting the service; Docker deployment has not been tested on this host.


## Startup inference and idle keep-warm

Before accepting traffic, the service performs internal inference through
Hindi, Marathi and English synthesis, SNAC decoding, speed processing and MP3 encoding. It runs a small maintenance pass after
120 seconds without client activity. Active requests skip idle maintenance;
incoming authenticated requests cancel maintenance before entering inference.
Any already running inference thread is allowed to finish safely. Dummy audio
and outputs are discarded without recording client requests, minutes or
transcripts. Client voice settings are unchanged.

Set `MODEL_KEEP_WARM_SECONDS` in the service environment to change the interval;
`0` disables idle refreshes while retaining startup inference. Startup failure
prevents serving; idle failures are logged and retried on the next interval.
`/admin/api/overview` includes warmup readiness, last success, run/failure/skip
counts and the interval. The journal records startup and idle completion times.
Existing systemd boot startup and automatic restarts also run this warmup.
Warmup reduces cold initialization overhead; model generation, queues and shared
GPU load still contribute to request latency. WebSocket and HTTP `stream:true`
deliver audio while the same phrase is still being generated.

## Native preset generation

Preset requests use the selected speaker label and original phrase text.
The automatic reference-conditioning experiment was withdrawn after the user
reported pronunciation regressions. Preset requests never acquire generated
audio references or reference transcripts. Explicit user-provided references
retain their existing path. Stable-volume processing applies one constant
gain per phrase; speed 1.0 retains native timing. Independent phrases may
still vary in voice character; that issue remains unresolved. ASR word
recognition and pitch measurements do not certify pronunciation or identity.

The user approved the single-generation Marathi female comparison. Only that
preset now uses paragraph handling by default, through both HTTP and WebSocket.
The launcher uses a 4096-token context with the same GPU memory quota. For that
preset, the planner permits up to 1000 characters and an estimated 160 spoken
units per generation, bounded by context, with an output budget up to available
context. Other presets and explicit user references retain their previous
200-character / 60-unit / 2048-output-token defaults. Caller-supplied larger
`chunk_size` and `max_tokens` options remain available for comparison requests.
The all-preset default rollout was rejected after Hindi male repeatedly looped
its final sentence, including punctuation and sampling-control experiments.

Longer text still splits and may vary in voice between generations. Non-streaming
HTTP buffers full phrases. Only that Marathi sample has user listening
approval. Unit routing coverage and representative live checks do not certify
pronunciation in every language or speaker. The universal voice/pronunciation
issue is not solved.

## Progressive WebSocket and HTTP audio

`/v1/audio/speech/ws` retains its existing request fields and JSON/binary frame
protocol. HTTP `stream:true` uses the same progressive path, with its existing
formats. The path keeps the same prompt, voice, sampling parameters and phrase
planner. It waits for 24 coarse codec frames, then sends 16 frames of PCM
(1.365 seconds) while retaining eight future frames as context.
Subsequent decodes emit 16 frames with eight frames of left and right context.
Aligned 20 ms blends smooth codec noise at boundaries without dropping or
repeating samples. The final flush retains all trailing frames. This replaces
the full-passage wait without independently regenerating small speech chunks.

Volume gain is estimated once from 2.048 seconds of already decoded initial
audio, including the future context, so starting earlier does not shorten its
calibration window. In seven representative language/speaker comparisons, the
calibration gain differed by less than 0.02 dB from the previous startup decode.
Gain stays fixed for the
phrase; later transients use a static smooth peak knee. It does not use packet
AGC. Speed 1.0 bypasses time stretching. Other speeds use a single streaming
FFmpeg atempo process per phrase. Disconnect cleanup closes the token source,
aborts unfinished vLLM work and terminates the tempo process.

HTTP encoding disables redundant PCM probing, flushes encoded packets promptly,
and uses 20 ms Ogg pages for Opus. Codec, bitrate, sample rate and generation
quality defaults remain unchanged. Regression tests decoded the new MP3, AAC,
Opus and WAV outputs to exactly the same samples as the previous encoder
commands on identical PCM. Encoder errors propagate to cleanup instead of
being swallowed. HTTP cancellation also closes the metering and model source.

Normal speed emits without an additional startup reserve. Faster playback
holds an initial reserve once per request, up to 2.4 output seconds at 2x, to
reduce gaps when playback approaches model throughput. This trades a little
first-audio latency for continuity at higher speeds; heavy load can still
outpace generation. In the initial WebSocket rollout, first audio fell from
24.065 seconds to 1.445 seconds at 1x, with no playback underruns. A Hindi male
request through the production TLS listener delivered first audio in 1.357
seconds, also without underruns.

The follow-up optimization starts its first decode at 24 rather than 32 frames.
On the approved Marathi passage, WebSocket first audio was 1.393 seconds and
1.050 seconds on a warm repeat, both without underruns. HTTP PCM streaming
fell from 25.747 to 1.217 seconds; MP3 first audio payload fell from 39.436 to
1.090 seconds. HTTP measurements exclude header-only output by waiting for
512 payload bytes. These measurements are not latency guarantees.

The same-code comparison retained all 712,704 samples, with waveform correlation
0.99972 to a full decode. The codec adds stochastic noise on every decode, so
byte equality is not expected. This check supports codec continuity; it does
not certify every preset's pronunciation. CPU decoding used about 5.53 seconds
for 29.70 seconds of audio on this machine. Measurements and sample audio are
in `quality-checks/streaming-latency-v2/`; the first rollout reports remain
in `quality-checks/websocket-latency/`. The full regression suite passed 42 tests.
