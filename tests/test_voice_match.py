"""A later phrase that comes out in a clearly different voice is generated again."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from tts_engine.orchestrator import SvaraTTSOrchestrator
from tts_engine.voice_match import VoiceMatcher, voice_profile

RATE = 24000


def voice(kind: str, seconds: float) -> bytes:
    """Two stand-in voices: a dark, steeply falling spectrum and a bright, flat one."""
    t = np.arange(int(RATE * seconds)) / RATE
    signal = sum(np.sin(2 * np.pi * 120 * k * t) / (k ** 2 if kind == 'dark' else 1.)
                 for k in range(1, 60))
    signal = signal / np.max(np.abs(signal)) * .5
    # Real recordings always have a noise floor; a noiseless tone has none.
    signal = signal + np.random.default_rng(0).normal(0, .002, signal.size)
    return (np.clip(signal, -1, 1) * 32767).astype('<i2').tobytes()


class MatcherTests(unittest.TestCase):
    def test_same_voice_matches_and_a_different_voice_does_not(self):
        matcher = VoiceMatcher()
        self.assertTrue(matcher.matches(voice('bright', 3)))  # no reference yet
        matcher.adopt(voice('dark', 6))
        self.assertTrue(matcher.matches(voice('dark', 3)))
        self.assertFalse(matcher.matches(voice('bright', 3)))
        self.assertIsNone(voice_profile(bytes(100)))
        self.assertTrue(matcher.matches(bytes(100)))  # too little audio to judge

    def test_loudness_does_not_change_the_voice(self):
        matcher = VoiceMatcher()
        matcher.adopt(voice('dark', 6))
        quiet = (np.frombuffer(voice('dark', 3), '<i2') * .1).astype('<i2').tobytes()
        self.assertTrue(matcher.matches(quiet))


class Codec:
    """Decodes any codes as the voice the stub transport last generated."""
    def __init__(self):
        self.voice = 'dark'
        self.decodes = []

    def decode_codes(self, codes):
        self.decodes.append(len(codes) // 7)
        return voice(self.voice, len(codes) // 7 * 2048 / RATE)


class Transport:
    def __init__(self, codec, voices):
        self.codec, self.voices, self.calls, self.closed = codec, list(voices), 0, 0

    async def astream(self, prompt, **kwargs):
        self.codec.voice = self.voices[min(self.calls, len(self.voices) - 1)]
        self.calls += 1
        try:
            for _ in range(60):
                yield ''.join(f'<custom_token_{10 + band * 4096 + 5}>' for band in range(7))
        finally:
            self.closed += 1


def build(voices):
    core = SvaraTTSOrchestrator.__new__(SvaraTTSOrchestrator)
    core.speaker_id = 'Marathi (Male)'
    core.max_chunk_chars = 200
    core.tokenizer = SimpleNamespace(encode=lambda *args, **kwargs: [1] * 40)
    core.codec = Codec()
    core.transport = Transport(core.codec, voices)
    return core


TEXT = 'First sentence is here. Second sentence is here.'


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def run_stream(self, core, **kwargs):
        with patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt'):
            return b''.join([part async for part in core.astream(
                TEXT, chunk_size=30, steady_volume=False, **kwargs)])

    async def test_a_different_later_voice_is_generated_again_before_any_of_it_is_sent(self):
        for progressive in (False, True):
            core = build(['dark', 'bright', 'dark'])
            output = await self.run_stream(core, progressive=progressive)
            self.assertEqual(core.transport.calls, 3, progressive)
            self.assertEqual(core.transport.closed, 3)
            frames = len(output) // (2048 * 2)
            self.assertEqual(frames, 120, progressive)
            matcher = VoiceMatcher()
            matcher.adopt(output[:60 * 4096])
            self.assertTrue(matcher.matches(output[60 * 4096:]), progressive)

    async def test_the_same_voice_and_the_first_phrase_cost_no_extra_generation(self):
        core = build(['dark', 'dark'])
        await self.run_stream(core, progressive=True)
        self.assertEqual(core.transport.calls, 2)

    async def test_retries_are_bounded_and_the_new_voice_then_becomes_the_reference(self):
        core = build(['dark', 'bright', 'bright', 'bright'])
        output = await self.run_stream(core, progressive=False)
        self.assertEqual(core.transport.calls, 4)  # first + three attempts
        self.assertEqual(len(output) // (2048 * 2), 120)

    async def test_it_can_be_disabled_and_is_skipped_for_deterministic_sampling(self):
        core = build(['dark', 'bright'])
        with patch.dict('os.environ', {'VOICE_MATCH_RETRIES': '0'}):
            await self.run_stream(core)
        self.assertEqual(core.transport.calls, 2)
        core = build(['dark', 'bright'])
        await self.run_stream(core, temperature=0)
        self.assertEqual(core.transport.calls, 2)

    async def test_the_long_generation_voice_is_not_checked(self):
        core = build(['dark', 'bright'])
        core.speaker_id = 'Marathi (Female)'
        core.max_paragraph_chars = 1000
        with patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt'):
            [part async for part in core.astream(
                TEXT, chunk_size=30, speaker_id='Marathi (Female)', steady_volume=False)]
        self.assertEqual(core.transport.calls, 2)

    async def test_a_single_phrase_or_reference_voice_is_never_checked(self):
        core = build(['dark'])
        with patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt'):
            [part async for part in core.astream('One short sentence.', steady_volume=False)]
        self.assertEqual(core.transport.calls, 1)
        core = build(['dark', 'bright'])
        with patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt'):
            [part async for part in core.astream(
                TEXT, chunk_size=30, steady_volume=False, audio_reference=[128266] * 7)]
        self.assertEqual(core.transport.calls, 2)


if __name__ == '__main__':
    unittest.main()
