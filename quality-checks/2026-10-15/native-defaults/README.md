# Scope of paragraph-mode validation and staged rollout

The user accepted the Marathi female single-generation comparison and asked
whether all speakers/languages were solved. The supported native API catalog
contains 38 male/female presets across 19 languages. Other catalog definitions
do not represent additional loaded models.

## Shared routing and live checks

31 unit/regression tests pass. Routing checks confirm explicit paragraph options
can select one native generation for each of the 38 presets, retain the selected
speaker and original text, avoid generated references, respect context/token
limits, and preserve native speed and warmup. These checks do not judge accents.

Six live requests were made with no chunk/token overrides during a provisional
global default rollout: female/male Marathi, Hindi and English. All six produced
one nonempty generation with no clipped samples. The recording and metadata for
each are retained here. Tail ASR checks identified a Hindi male failure: it
repeated its last sentence several times and reached the generation budget.
Both an ASCII-punctuation trial and a lower-temperature / higher-repetition-
penalty trial reproduced the same failure. Their settings were not adopted.

The global default rollout was therefore withdrawn. The approved Marathi female
profile alone now defaults to the larger paragraph mode. Other profiles retain
their previous defaults; larger options remain available for explicit trials.
`rejected-global-defaults.py` archives the provisional implementation.

## Accepted scope

Only the Marathi female sample has user listening approval. Other successful
flow/content checks are not pronunciation or speaker-identity certification.
The universal issue remains unresolved. Longer inputs still require separate
generations and may change voice at boundaries even in the approved profile.
The larger mode materially delays first playback (about 19 seconds for the
approved Marathi passage). API paths, native voice labels, and speed are kept.
