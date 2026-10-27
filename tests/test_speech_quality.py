"""Regression checks for frame alignment, retained phonemes and speaking pace."""

import asyncio
import unittest

import numpy as np

from tts_engine.mapper import SvaraFrameParser, SvaraMapper
from tts_engine.pacing import phrase_chunks, prepare_phrase, speech_units


def tokens(frames):
    return ''.join(f'<custom_token_{10 + band * 4096 + value}>'
                   for frame in frames for band, value in enumerate(frame))


class FrameTests(unittest.TestCase):
    def test_zero_codes_and_fragmented_transport_deltas_survive(self):
        frames = [[0, 20, 30, 0, 50, 60, 70], [10, 0, 30, 40, 0, 60, 0]]
        source = tokens(frames)
        parser = SvaraFrameParser()
        for index in range(0, len(source), 5):
            parser.feed_text(source[index:index + 5])
        self.assertEqual(parser.codes, sum(frames, []))
        mapper = SvaraMapper(window_size=7)
        for band, value in enumerate(frames[0]):
            result = mapper.feed_raw(10 + band * 4096 + value)
        self.assertEqual(result, frames[0])

    def test_damaged_frame_recovers_at_the_next_frame_boundary(self):
        parser = SvaraFrameParser()
        parser.feed_text('<custom_token_10><custom_token_8202>')
        parser.feed_text(tokens([[1, 2, 3, 4, 5, 6, 7]]))
        self.assertEqual(parser.codes, [1, 2, 3, 4, 5, 6, 7])

    def test_complete_phrase_decode_retains_start_and_end_frames(self):
        import torch
        from tts_engine.codec import SNACCodec

        class FakeModel:
            def decode(self, codes):
                length = codes[0].shape[1] * 2048
                return torch.linspace(-.25, .25, length).reshape(1, 1, -1)

        codec = SNACCodec.__new__(SNACCodec)
        codec.device = 'cpu'
        codec.model = FakeModel()
        output = codec.decode_codes([0] * 28)
        samples = np.frombuffer(output, dtype='<i2')
        self.assertEqual(len(samples), 4 * 2048)
        self.assertLess(samples[0], 0)
        self.assertGreater(samples[-1], 0)

    def test_phrase_grouping_keeps_words_and_emotion_tags(self):
        text = 'नमस्कार! आज तुम्ही कसे आहात? <happy> तुमचे स्वागत आहे.'
        phrases = phrase_chunks(text, 70)
        self.assertEqual(' '.join(phrases), text)
        self.assertFalse(any(part.startswith('<happy>') for part in phrases))
        self.assertLess(speech_units('नमस्कार'), len('नमस्कार'))


class PaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_request_levels_separately_generated_phrases(self):
        from tts_engine.orchestrator import SvaraTTSOrchestrator

        class StubOrchestrator(SvaraTTSOrchestrator):
            def __init__(self):
                self.max_chunk_chars = 50

            async def _astream_one(self, text, **kwargs):
                amplitude = .015 if text.startswith('First') else .15
                phase = np.arange(48000) / 24000
                yield (np.sin(2 * np.pi * 190 * phase) * amplitude * 32767).astype('<i2').tobytes()

        text = ('First sentence is softly spoken here. '
                'Second sentence is loudly spoken here.')
        for enabled in (False, True):
            output = b''.join([part async for part in StubOrchestrator().astream(
                text, speed=1., steady_volume=enabled)])
            self.assertEqual(len(output), 192000)
            samples = np.frombuffer(output, dtype='<i2').astype(float)
            early = np.sqrt(np.mean(samples[:48000] ** 2))
            later = np.sqrt(np.mean(samples[48000:] ** 2))
            if enabled:
                self.assertAlmostEqual(20 * np.log10(later / early), 0., delta=.3)
            else:
                self.assertAlmostEqual(later / early, 10., delta=.05)

    async def test_speed_changes_duration_without_changing_pitch(self):
        rate = 24000
        phase = np.arange(rate * 3) / rate
        pcm = (np.sin(2 * np.pi * 220 * phase) * 10000).astype('<i2').tobytes()
        outputs = [await prepare_phrase(pcm, 'आज तुम्ही कसे आहात तुमचे स्वागत आहे', speed, False)
                   for speed in (.5, 1., 2.)]
        durations = [len(output) / 48000 for output in outputs]
        self.assertAlmostEqual(durations[0] / durations[1], 2., delta=.05)
        self.assertAlmostEqual(durations[1] / durations[2], 2., delta=.05)
        for output in outputs:
            samples = np.frombuffer(output, dtype='<i2').astype(float)
            spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
            pitch = np.argmax(spectrum) * rate / len(samples)
            self.assertAlmostEqual(pitch, 220, delta=2)

    async def test_normal_speed_retains_the_exact_voice_waveform_and_pauses(self):
        tone = (np.sin(2 * np.pi * 200 * np.arange(24000) / 24000) * 12000).astype('<i2').tobytes()
        pcm = bytes(4800) + tone + bytes(48000) + tone + bytes(9600)
        for text in ('नमस्कार आज तुम्ही कसे आहात', 'Hello, welcome to our service'):
            output = await prepare_phrase(pcm, text, 1., False)
            self.assertEqual(output, pcm)

    async def test_stable_volume_retains_voice_spectrum_and_duration(self):
        phase = np.arange(48000) / 24000
        signal = np.sin(2 * np.pi * 190 * phase) + .3 * np.sin(2 * np.pi * 570 * phase)
        pcm = (signal * 5000).astype('<i2').tobytes()
        output = await prepare_phrase(pcm, 'नमस्ते आपका स्वागत है', 1., True)
        self.assertEqual(len(output), len(pcm))
        original = np.frombuffer(pcm, dtype='<i2').astype(float)
        processed = np.frombuffer(output, dtype='<i2').astype(float)
        self.assertGreater(np.corrcoef(original, processed)[0, 1], .999999)


if __name__ == '__main__':
    unittest.main()
