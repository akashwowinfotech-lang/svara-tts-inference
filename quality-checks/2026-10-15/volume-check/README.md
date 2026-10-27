# Volume change in speech(1).wav

Source: `/home/wowbharat/Downloads/speech(1).wav`, 34.9013 seconds, mono PCM16 at 24 kHz.

There is a sustained level change near 20–21 seconds. The adaptive gated
speech RMS differs by 12.20 dB between the opening and closing sections.
Three-second windows also show a sustained difference, rather than an
isolated stressed syllable. The source has no full-scale clipping.

The prior phrase normalizer used an absolute 0.025 RMS gate and a maximum
gain of 3.5. This could ignore quieter parts of a synthesized phrase and
leave quiet phrases below the following phrase. The correction uses a
relative speech gate and up to 16x gain with a peak headroom constraint.
It applies one constant gain per complete phrase; it adds no compression,
equalization, gain ramp, pitch change, resampling, or pause edits.
The stable-volume opt-out remains available.

## Controlled sample comparison

`volume-balanced-sample.wav` applies the new volume algorithm to two sections
of the supplied waveform, split at 20.3 seconds inside its measured quiet
gap (19.96–20.64 seconds). This is a diagnostic split; the original model
phrase boundaries and input text are unavailable. The source is unchanged.

| Measurement | Opening section | Closing section |
| --- | --- | --- |
| Original speech RMS | -31.13 dBFS | -18.93 dBFS |
| Balanced speech RMS | -18.42 dBFS | -18.42 dBFS |
| Original/balanced waveform correlation | 0.999999998 | 0.999999997 |

Duration is unchanged and there are no clipped samples. Constant gain
preserves pitch, spectrum shape and within-section prosody. This is signal
verification, not a human listening assessment of speaker identity.

All 28 TTS regression tests pass, including a long-request integration test
with two generated phrases differing by 20 dB. Stable volume brings those
phrases within 0.3 dB; disabling it retains their original amplitude ratio.
Existing tests cover native speed, pitch-preserving speed changes, frame
alignment, access controls and warmup lifecycle behavior.

## Live service verification with the user's exact text

After deployment, the user-provided Marathi nature passage was generated over
the production application's WebSocket endpoint with `mr_female`, speed 1.0
and stable volume enabled. `live-mr-female.wav` contains the returned audio.
The three generated phrases measured -18.416, -18.416 and -18.683 dBFS gated
speech RMS: a range of 0.267 dB, with no clipped samples. The last phrase's
peak constraint retained headroom rather than forcing additional gain.
The output lasts 35.84 seconds; independent model sampling accounts for its
different duration from the supplied source. This is not a pointwise
comparison between separate model generations.

The restarted service completed its normal startup warmup in 4.097 seconds
and its public HTTPS health check returned healthy. Only TTS was restarted.
