# Server warmup verification

Checked: 2026-10-15T11:00:20.444337+05:30

Regression suites: TTS 24, STT 18, multispeaker STT 9; all 51 passed.

## speech-api.service

- HTTPS health: healthy
- Systemd: active; boot: enabled
- Idle interval: 120 seconds
- Warmup errors: 0
- Usage observation: 3 real client request records were added during validation; TTS HTTP access logs independently confirmed the three successful speech requests. Internal warmup calls bypass endpoint usage logging.

```text
2026-10-15T10:51:06+05:30 ganas-ai-server run-local.sh[211468]: [2026-10-15 10:51:06] INFO warmup.py:64: Startup warmup completed in 3.606s
2026-10-15T10:53:06+05:30 ganas-ai-server run-local.sh[211468]: [2026-10-15 10:53:06] INFO warmup.py:64: Idle warmup completed in 0.574s
2026-10-15T10:58:07+05:30 ganas-ai-server run-local.sh[211468]: [2026-10-15 10:58:07] INFO warmup.py:64: Idle warmup completed in 0.574s
```

## stt-api.service

- HTTPS ready: ready
- Systemd: active; boot: enabled
- Idle interval: 150 seconds
- Warmup errors: 0
- Usage observation: No new client usage records during idle validation.

```text
2026-10-15T10:56:33+05:30 ganas-ai-server uvicorn[213213]: INFO:stt.warmup:Startup warmup completed in 0.498s
2026-10-15T10:59:33+05:30 ganas-ai-server uvicorn[213213]: INFO:stt.warmup:Idle warmup completed in 0.113s
```

## multispeaker-stt.service

- HTTPS ready: ready
- Systemd: active; boot: enabled
- Idle interval: 180 seconds
- Warmup errors: 0
- Usage observation: No new client usage records during idle validation.

```text
2026-10-15T10:56:15+05:30 ganas-ai-server uvicorn[213214]: INFO:multispeaker-stt.warmup:Startup warmup completed in 0.383s
2026-10-15T10:59:15+05:30 ganas-ai-server uvicorn[213214]: INFO:multispeaker-stt.warmup:Idle warmup completed in 0.148s
```
