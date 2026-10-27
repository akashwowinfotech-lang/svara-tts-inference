# Native single-generation Marathi comparison

The user identified the first four sentences as acceptable and the final two
sentences, starting with “जर आपण”, as changing voice and pronunciation.
Those final sentences formed a separate native generation in the prior path.
Automatic voice-reference conditioning was previously rejected and remains
removed.

## Comparison method

The installed model config supports a context larger than 4096 tokens. Raised
the local service context from 2048 to 4096 while retaining its 0.55 GPU memory
quota, model, preset, sampling defaults and speed 1.0. The existing request
fields `chunk_size=1000` and `max_tokens=4096` now permit this 155-unit paragraph
to fit in one generation. Larger requested chunks have a bounded unit budget;
ordinary requests retain their default 200-character / 60-unit planning.
No endpoint, schema field or automatic voice cloning was added.

`marathi-one-generation.wav` contains the exact full paragraph from one native
`mr_female` model generation. The generated audio represents 2436 SNAC tokens,
which explains why the old 2048-token service context could not hold the full
speech and prompt together.

## Results and limits

- Output duration: 29.696 seconds.
- First audio in this test: 19.085 seconds; total request: 19.132 seconds.
- One generated phrase, with no generation restart at the reported sentence.
- No clipped samples; volume processing uses one gain over the complete output.
- 5-second gated speech levels span about 2.3 dB, rather than the prior 12 dB jump.
- ASR recognizes the full passage and ending; this is only a content sanity check.
- All 30 TTS regression tests passed. Startup warmup passed in 3.565 seconds.

This is a listening comparison, not a pronunciation or speaker-identity
certification. The latency cost is material: complete-context decoding and
whole-phrase volume measurement delay the first packet until generation ends.
This larger-paragraph path is available through explicit existing request
options; it has not replaced the normal default planner while human listening
feedback on the comparison is pending. Longer recordings still require splits
and can still exhibit variation between independent model generations.
