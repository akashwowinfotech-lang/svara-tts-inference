> Superseded by revision 2: automatic target-duration stretching and pause compression were removed after the user reported a change in voice character. The earlier duration-equality results below describe the rejected first revision, not the current native-voice behavior. See `revision-2/README.md`.

# TTS quality verification — 15 October 2026

The live TTS service uses complete phrase decoding, aligned SNAC frames,
phrase loudness estimation, pitch-preserving tempo correction, and buffered
browser playback. Speech endpoint paths and output formats are preserved.

## Matched Hindi and Marathi pacing

The two translated test passages each have 45 estimated syllabic units.
Sampling temperature was zero for the before/after comparison.

| Voice | Before at speed 1.0 | After at speed 1.0 |
| --- | --- | --- |
| hi_female | 9.301 s | 9.766 s |
| mr_female | 9.813 s | 9.758 s |
| hi_male | 8.448 s | 9.757 s |
| mr_male | 8.107 s | 9.766 s |

Marathi female: speed 0.75 produced 13.005 seconds; speed 1.25 produced
7.807 seconds. The duration ratio matches the requested speed ratio.
HTTP and WebSocket speed tests both passed. WAV and MP3 outputs were
verified as mono 24 kHz audio.

## Continuity and coverage

- Generated nonempty PCM with all 38 preset voices across 19 language profiles.
- The Bodo profile check used a greeting and phonetic test syllables; it was
  a generation check rather than a native-language pronunciation evaluation.
- A 39.06-second Marathi stream had no measured playback
  underrun with a 600 ms playback queue.
- All 19 automated tests passed, including zero-code alignment, fragmented
  token parsing, retained opening/final codec frames, pitch preservation,
  pace correction, and existing administration tests.
- A player scheduling check verified that playback waits for its queue and
  schedules successive buffers without adding per-piece delays.

## Limits and review samples

Speaking pace uses a script-aware syllable estimate and bounded corrections;
it is not a pronunciation or phoneme alignment engine. Very short utterances,
expressive styles, and intrinsic model pronunciation can still vary. Generation
and signal checks do not establish human-rated naturalness or perfect speaker
identity. Listen to the paired before/after WAV files for that assessment.

Complete phrase preparation trades first-audio latency for continuity. The
matched Hindi/Marathi requests started after roughly 5–6 seconds on this host;
shorter phrases and load change that delay. No STT code or service was changed.
