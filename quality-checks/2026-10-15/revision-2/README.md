# Revision 2 — preserve native voice character

This revision supersedes the automatic duration-normalization approach.
The user reported that the resulting voices sounded changed. Equal duration
was not a valid acceptance test for naturalness or speaker identity.

Removed automatic syllable-derived tempo correction and silence compression.
At speed 1.0, decoded PCM bypasses time stretching entirely. Explicit speed
multipliers still use pitch-preserving tempo processing. Restored the previous
sampling temperature of 0.75 without forcing a seed. Retained frame alignment,
complete-context decoding, and buffered playback.

## Controlled comparison

Each native waveform was processed at speed 1.0 with and without stable volume.
The unadjusted path preserved the input bytes exactly, including all pauses.
Stable volume kept the same duration and changed only amplitude:

| Voice | Native duration | Raw/level-adjusted waveform correlation |
| --- | --- | --- |
| hi_female | 9.557 s | 0.999999972 |
| mr_female | 10.155 s | 0.999999995 |

The aligned Hindi native waveform correlated 0.9964 with the pre-change
baseline, and its opening correlated 0.9994. The retained start/end codec
frames account for 256 ms of extra audio. These are signal comparisons, not
human speaker-identity ratings. The independently generated Marathi request
had a different length, so no pointwise baseline correlation is claimed.

An initial comparison of two independently generated Hindi requests had
different lengths even at temperature zero. That comparison cannot isolate
processing effects; the controlled checks above used identical audio input.

All 19 unit tests passed. Explicit speed ratios passed on the same native
Marathi waveform. HTTP endpoint paths and audio formats are preserved.

## Scope

Normal speed now preserves each preset's native cadence. The rejected first
revision's claim of common absolute pace across languages is withdrawn.
Hindi and Marathi preset IDs are separate voice profiles; a requirement for
the same person's identity across languages needs a shared reference voice.
The corrected WAV files here allow a listening comparison. No human-rated
naturalness assessment has been performed by the agent.
