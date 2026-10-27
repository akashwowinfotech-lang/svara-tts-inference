# STT parallel inference rollout — 27 October 2026

STT now processes compatible utterances together in native GPU batches of up to two. The existing HTTP, PCM WebSocket and telephony endpoints and response protocols are preserved. Eight active client requests/sessions remain the global admission limit; extra utterances still queue behind the current batch.

## Implementation

- A single dedicated worker groups compatible jobs for up to 15 ms when idle. It performs one encoder/projector/decoder call with a separate tensor row for each recording. This avoids independent concurrent calls into the non-reentrant decoder.
- Default parameters are normalized so HTTP and streaming requests can share a batch. Different generation settings are processed separately.
- Per-recording confidence, EOS handling, repetition checks and script checks stay separate. Model precision and generation controls are preserved.
- Batches are capped at two for the current shared GPU allocation. A two-item GPU allocation failure retries each recording separately and disables batching until restart. Single-item allocation failures still propagate; this is not a guarantee against all GPU resource exhaustion.
- `/ready` exposes current batch size, memory fallback count, completed batch passes and batched utterance count.
- `/etc/systemd/system/stt-api.service.d/parallel-inference.conf` sets `STT_INFERENCE_BATCH_SIZE=2`. The enabled service and checked-in deployment template retain this setting after reboot. Setting it to one and restarting STT restores serial inference.

## Measured HTTP results

These are short synthetic recordings and individual burst observations, not production percentiles or guaranteed capacity.

| Parallel requests | Before: slowest response | After: slowest response |
|---:|---:|---:|
| 1 | 0.388 s | 0.415 s |
| 2 | 0.744 s | 0.498 s |
| 4 | 1.473 s | 0.969 s |
| 6 | Not recorded in this comparison | 1.408 s |

The four-request burst completed about 34% faster. A single request can incur the batch collection delay and is not expected to benefit from batching.

Four reference recordings (English, Marathi, Kannada and Telugu) returned exactly the same transcript strings before and after the change. The final native validation recorded eight actual two-item GPU passes across the reference/HTTP burst tests, with zero GPU memory fallbacks. This is a regression sample, not a full accuracy evaluation across accents, languages, noise or longer recordings.

## Streaming and shared GPU checks

- Two real-time PCM WebSocket streams: final transcripts approximately 0.43–0.47 s after audio upload ended; one native batch completed in each test.
- Four real-time PCM streams: slowest final transcript was 1.27 s after audio ended on repeat. An earlier run reached 2.05 s. Both runs used real two-item batches and completed successfully. Timing remains sensitive to shared load.
- Mixed HTTP and WebSocket requests completed successfully, using the same endpoints and existing authentication.
- With a TTS request deliberately overlapping the STT decoding stage, both STT streams completed approximately 1.11 s after audio ended. TTS returned first audio in 1.05 s and completed successfully, with no simulated playback underrun.
- The tests use explicit `end` flushing. Normal silence endpointing can add approximately 700 ms after speech ends. Remote network latency is not established by requests originating from the server.

One separate TTS restart occurred during validation. Its first startup overlapped an STT restart and hit a GPU memory profiling assertion; its automatic retry recovered. No TTS source, configuration or restart command was changed by this rollout. The final overlapping audio test verified successful synthesis as well as STT decoding. Shared-GPU startup ordering and active traffic are additional reasons these observations are not controlled production throughput guarantees.

## Verification and recovery

All 25 installed regression tests passed, including native tensor row isolation, default compatibility across protocols, incompatible-settings separation, worker exception recovery, memory fallback, authentication, tenant separation and WebSocket handling. The real service reported CUDA readiness with batch size two and no memory fallbacks. Temporary validation keys were revoked.

The public HTML and Markdown integration guides now document automatic two-item batching, endpoint usage and readiness fields. No client request changes are required.

Original serial implementation backup: `/home/wowbharat/WOWBHARAT/STT/.backups/parallel-20261027-110946`. The subsequent protocol-normalization/documentation backup is `/home/wowbharat/WOWBHARAT/STT/.backups/parallel-final-20261027-111258`. Both installers included rollback if readiness or transcript/memory/HTTP-latency validation failed.

Artifacts: `before.json`, `after.json`, `streams.json` (last repeat), `shared-gpu.json`, and the three validation scripts. Recordings and transcripts used in this report are synthetic test material. Running these scripts again generates actual API load and temporary keys.
