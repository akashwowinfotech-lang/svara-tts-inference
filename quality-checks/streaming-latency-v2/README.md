# Lower startup latency for both streaming transports

Deployed to the existing `speech-api.service`. API URLs and request fields are
unchanged. HTTP requires `stream:true`; non-streaming HTTP still waits for the
complete output. No model, preset, sampling, token budget, phrase planning,
sample rate, bitrate or codec quality setting was reduced.

## Measured first audio

All requests used the same approved Marathi nature passage and female preset
at speed 1.0. Each request is a separate ordinary model generation, so total
audio duration and completion time vary. These are measurements at this server's
test load, not latency guarantees.

| Transport | Previous first audio | Updated first audio |
| --- | ---: | ---: |
| WebSocket PCM | 1.445 s from initial streaming rollout | 1.393 s; warm repeat 1.050 s |
| HTTP streaming PCM | 25.747 s | 1.217 s |
| HTTP streaming MP3 | 39.436 s | 1.090 s |

WebSocket timing counts the first binary audio frame, not the JSON start event.
Both updated WebSocket checks had no playback underruns with the same 120 ms
playback scheduling margin used in the original check. HTTP timing counts the
arrival of at least 512 bytes, avoiding a misleading measurement of just a
format header. Raw packet timing and audio files accompany each JSON report.

## Changes and quality checks

- First decode starts at 24 rather than 32 coarse codec frames. It emits 16
  frames (1.365 s), keeping eight future frames of codec context. The initial
  volume calibration still uses 2.048 s of already decoded audio, including
  the future context, and keeps one fixed gain for the phrase.
- HTTP streaming uses the same incremental model/codec path as WebSocket.
- Encoding avoids redundant PCM probing and flushes packets promptly. Opus
  uses shorter Ogg pages. Existing encoder quality defaults are unchanged.
- Cleanup closes the HTTP metering source and unfinished model generation;
  encoding failures propagate instead of producing silent truncation.
- Seven representative language/speaker checks had initial waveform
  correlations above 0.99974 and calibration gain changes below 0.02 dB.
- Full-passage decoding retained every one of 712,704 samples. Correlation
  with full decoding was 0.99972; no startup/end samples were lost, repeated
  or clipped. The small difference includes the codec's stochastic breath
  noise. No seed or automatic voice reference was added.
- MP3, AAC, Opus and WAV regression outputs decoded to exactly the same
  PCM samples as the previous encoder commands on identical input PCM.
- All 42 regression tests passed. Production health was healthy, and no
  generation or cleanup errors appeared in the benchmark request logs.

These waveform and format checks support continuity and retained encoding
quality. They do not independently certify pronunciation for every voice.
The existing faster-speed startup reserve remains in place. Client players
must consume chunks progressively to benefit from the earlier server output.

## Reproduce

From the project root, using the existing loopback-only owner key:

```bash
.venv/bin/python quality-checks/streaming-latency-v2/benchmark_ws.py repeat-ws
.venv/bin/python quality-checks/streaming-latency-v2/benchmark_http.py repeat-pcm pcm
.venv/bin/python quality-checks/streaming-latency-v2/benchmark_http.py repeat-mp3 mp3
```

Run benchmarks sequentially. Key values never appear in reports. The CPU thread
benchmark found the existing 16-thread setting faster than smaller pools for
this codec window, so thread settings were left unchanged. GPU configuration
and other speech services were also left unchanged.
