# Client priority verification — 27 October 2026

Deployed High (1), Normal (2), Low (3) priority for client keys. Existing keys migrate to Normal. Priority changes apply to new requests. The shared HTTP/WebSocket admission queue chooses the next eligible waiting request; running generation and audio are not preempted.

The live test occupied four server slots using temporary localhost-only keys, then submitted Low HTTP, Normal WebSocket, High HTTP requests, in that order. Freeing one slot admitted High, then Normal, then Low. Every priority test request completed with valid nonempty PCM. A disconnected WebSocket waiter was removed before admission. Final active and queued counts were both zero. Temporary keys were revoked and removed, and the temporary dashboard session was logged out.

The test also verified creating priorities, editing a temporary key, listing the saved value, and rejecting an invalid priority with HTTP 422. Raw event timing and byte counts are in `live-report.json`. `live_check.py` contains the repeatable check without credentials. It requires an idle server and intentionally uses the current four-slot setting; it is not a general production load generator.

28 focused unit checks passed: 12 queue/admission/storage checks, eight access checks, three dashboard validation checks, five maintenance lifecycle checks. Cases cover migration, persistence, FIFO ties, aging, blocked-key bypass, shared capacity, global/per-key queue bounds, rate limits, timeout, cancellation immediately after reservation, disconnection, and revocation during waiting. Python compilation, JavaScript syntax and whitespace checks passed. Public HTTPS health and the served dashboard JavaScript were checked after deployment.

Defaults: four active streams, 64 waiting globally, eight waiting per key, 30-second wait timeout, and one priority-level improvement per ten seconds of waiting (effective priority bottoms at zero). A full queue, rate limit or wait timeout is rejected with HTTP 429 or the existing WebSocket error mechanism. Queue wait counts toward time to first audio. Priority does not change audio quality settings or guarantee a latency target.

A private database backup was saved under `.data` before schema migration. Runtime settings and operations are documented in `PRODUCTION.md`; developer-facing queuing behavior is documented in `PUBLIC_API.md` and the public integration guide.
