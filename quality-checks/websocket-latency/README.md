# WebSocket latency check

The original approved Marathi nature passage is read from
`../2026-10-15/single-pass/verification.json`. Requests use the existing local
owner key; credentials are never included in reports. Each WAV is a separate
ordinary model generation with unchanged sampling controls, so durations vary.

| Check | First audio | Completion | Output audio | Playback gaps |
| --- | ---: | ---: | ---: | --- |
| Before, Marathi female 1x | 24.065 s | 24.113 s | 30.805 s | Buffered full passage |
| Progressive, Marathi female 1x | 1.445 s | 15.780 s | 29.696 s | None |
| Production TLS listener, Hindi male 1x | 1.357 s | 5.485 s | 11.008 s | None |
| Final startup reserve, Marathi female 2x | 3.635 s | 16.186 s | 14.848 s | None |

The TLS check connects to the local production Nginx listener while retaining
the public hostname and certificate verification. The owner key allows only
loopback; a request routed via public DNS was correctly denied by its IP policy.
No key permissions were broadened.

`before.json`, `after.json`, and `after-tls.json` contain packet arrival times.
The playback check schedules received PCM in order with a 120 ms starting
margin, and records any time arrival catches up with the queued playback end.
Measurements are from this server and load, not a latency guarantee.

The first 2x test (`after-speed2.json`) exposed brief playback gaps. The final
implementation keeps an initial reserve at higher speeds, up to 2.4 seconds of
output at 2x. This reserve is chosen once per request, not per packet. The final
live check (`after-speed2-buffered.json`) had no playback gaps.

`progressive-codec-verification.json` compares progressive and full decoding
of identical saved codes, avoiding model generation variability. Both retained
712,704 samples, with correlation 0.99973. Decode windows used bounded context,
20 ms aligned blends, and no deleted or repeated samples. Boundary jumps were
below ordinary waveform sample differences. The codec introduces stochastic
breath noise, accounting for small waveform differences. No RNG seed or voice
reference was introduced. These checks do not certify pronunciation.

To repeat from the project root (real network/thread execution is required):

```bash
.venv/bin/python quality-checks/websocket-latency/benchmark_ws.py repeat
TTS_PROBE_LOCAL_TLS=1 .venv/bin/python quality-checks/websocket-latency/benchmark_ws.py repeat-tls wss://api.tts.ganas.ai/v1/audio/speech/ws 1 hi_male
```

The full suite passed 38 tests, covering frame alignment, early output before
model completion, cancellation, tempo continuity and pitch, fixed gain, native
voice routing, warmup, authentication and existing admin behavior.
