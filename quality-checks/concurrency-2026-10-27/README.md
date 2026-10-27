# Live speech concurrency audit — 27 October 2026

Both APIs accept parallel connections and return valid results for admitted requests. Latency increases under load. The current deployment does not provide uniformly low latency for simultaneous requests.

## Measured results

These are observed maxima in a small controlled run, not statistically established production p95 values.

| Workload | Test requests | Completed | Maximum first audio / response delay | Maximum total request time |
|---|---:|---:|---:|---:|
| TTS WSS | 1 | 1 | First audio 1.215 s | 2.899 s |
| TTS WSS | 2 | 2 | First audio 1.601 s | 3.360 s |
| TTS WSS | 4 | 4 | First audio 2.247 s | 4.860 s |
| TTS HTTP PCM streaming | 4 | 4 | First audio 1.990 s | 5.859 s |
| STT HTTP file | 1 | 1 | Entire response 0.404 s | 0.404 s |
| STT HTTP file | 2 | 2 | Entire response 0.771 s | 0.771 s |
| STT HTTP file | 4 | 4 | Entire response 1.524 s | 1.524 s |
| STT HTTP file | 8 | 6; 2 busy responses | Entire response 2.305 s among admitted requests | 2.305 s |
| STT real-time WSS | 1 | 1 | Final transcript 0.343 s after audio upload ends | 5.467 s including real-time audio |
| STT real-time WSS | 4 | 4 | Final transcript 1.388 s after audio upload ends | 6.564 s including real-time audio |
| STT real-time WSS | 8 | 6; 2 busy responses | Final transcript 2.081 s after audio upload ends | 7.301 s including real-time audio |
| Combined: TTS WSS, alongside 4 STT WSS streams | 4 | 4 | First audio 2.077 s | 4.940 s |
| Combined: STT WSS, alongside 4 TTS WSS streams | 4 | 4 | Final transcript 1.373 s after audio upload ends | 6.586 s including real-time audio |

47 test requests: 43 completed successfully and 4 were rejected by STT admission limits with 429. No admitted test request failed inference. All 87 health probes during the individual workload phases succeeded; the slowest pair of concurrent health probes completed in 51 ms. Combined workload phases did not include these probes. Temporary audit keys were revoked after completion. Both services remained active and ready.

## Interpretation

- **TTS:** Four requests can generate concurrently using the asynchronous engine. First audio rose from approximately 1.2 s for a single request to 2.25 s for four. A playback simulation using a 120 ms initial buffer predicted up to 0.404 s cumulative underrun per four-way WSS stream and 0.797 s for HTTP streaming. These are potential playback stalls; actual browser playback was not listened to in this audit and depends on client buffering. One- and two-way WSS tests showed no simulated underruns.
- **STT:** The global cap is eight active requests/sessions, while one GPU transcription runs at a time. The implementation also locks the single model instance. More connections therefore do not mean parallel GPU decodes. Four simultaneous utterances incurred up to 1.035 s queue wait. Six admitted concurrent streams incurred up to 1.727 s queue wait. Increasing the connection cap alone will increase waiting rather than remove this bottleneck.
- **Existing traffic:** A live admin overview confirmed two STT sessions were already active outside the audit. Eight additional test requests could therefore only occupy six free slots, explaining the controlled 429 responses. Other live traffic was not stopped. TTS also had two active sessions in the later overview snapshot, although all four-request TTS test phases were admitted.
- **Shared resources:** Both services share the RTX 5090. Approximately 31.0 GB of 32.6 GB GPU memory was allocated before and after the test. There is little headroom for an extra model replica with the current allocation.
- **Historical error handling:** A sampled STT traceback from the preceding 24 hours showed `ClientDisconnected` while closing an already disconnected WebSocket in `server/control.py:157`. That path is classified as a 500 in usage reports. Disconnect cleanup should distinguish normal transport closure from inference failure. This did not cause an admitted benchmark request to fail.

## Scope and limits

The audit used the public HTTPS/WSS endpoints from the server itself, so it includes TLS/proxy processing but is not an independent remote-network latency test. TTS input was a short English sentence; its generated approximately five-second audio was used as the STT fixture. WSS STT audio was sent at real-time speed and explicitly flushed after upload. Production silence detection can add roughly 700 ms after the last spoken word when callers do not explicitly flush. Longer utterances, other languages, mobile networks and prolonged traffic can perform differently.

This was a burst audit, not a sustained capacity or quality evaluation. Repeated inputs cannot demonstrate transcript isolation between different spoken contents. The raw JSON contains per-request observations and small-sample p50/p95 summaries; the table uses maxima to avoid treating these small samples as production percentiles.

## Next performance work

Profile and optimize the TTS progressive decode/packet schedule to prevent playback starvation under four-way load. For STT, investigate proper batched inference or additional GPU capacity, then benchmark real recordings before changing admission limits. Correct disconnect classification independently. Preserve the current endpoints and response protocols.

## Artifacts

- `results.json`: Per-request observations and health-probe timings.
- `benchmark.py`: Bounded test harness. It creates temporary IP-restricted API keys and revokes them in a finalizer. Running it again generates real load and usage records.
- `fixture.wav` / `fixture.pcm`: Synthetic test speech, with no customer audio.

No application code, service settings or endpoints were changed or restarted for this audit.
