# Restore native pronunciation after reference-conditioning regression

The user rejected the automatic-reference output because its pronunciation
was wrong. Removed the automatic generated-audio reference, copied transcript,
extra preset prefixes in continuation turns, and the short-opening planner.
Preset requests again use the original phrase text and selected native
speaker label. Explicit user-requested references remain supported. Retained
volume normalization, speed 1.0 waveform timing, and server warmup.

The earlier ASR matches verified recognizable words, not acceptable phonemes,
accent, prosody, or speaker identity. Those checks did not justify declaring
the voice issue solved. User listening feedback supersedes that conclusion.
The original speaker variation between independently generated phrases is
still unresolved; this rollback does not claim to fix that limitation.

The rejected implementation and its tests are archived here for comparison.
A regression check now verifies that long preset requests never acquire
automatic audio references or rewritten input phrases.

## Deployment and recovery check

All 29 TTS tests pass. TTS was restarted, completed startup inference in
3.668 seconds, and passed its HTTPS health check. The complete original
Marathi passage was generated again over the live WebSocket route and saved
as `native-marathi-restored.wav`. It uses three native preset phrases, lasts
35.84 seconds, and has no clipped samples. The rejected automatic-reference
output lasted 40.363 seconds. `native-comparison.json` compares the restored
output to the previous native-preset recording from before the regression.
This comparison establishes recovery of that synthesis path, not a human
pronunciation assessment or a solution for voice changes across phrases.
