"""Behavior checks for the streaming speech level stabilizer."""

import unittest

import numpy as np

from tts_engine.loudness import PhraseLoudnessNormalizer, StreamingLoudnessNormalizer


SAMPLE_RATE = 24000


def tone(amplitude: float, seconds: float = 0.5) -> bytes:
    samples = np.arange(int(SAMPLE_RATE * seconds))
    waveform = amplitude * np.sin(2 * np.pi * 220 * samples / SAMPLE_RATE)
    return (waveform * 32767).astype("<i2").tobytes()


def rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    return float(np.sqrt(np.mean(samples * samples)))


class StreamingLoudnessTests(unittest.TestCase):
    def test_reduces_segment_level_change_without_clipping(self):
        normalizer = StreamingLoudnessNormalizer()
        quiet = [normalizer.process(tone(0.04)) for _ in range(4)]
        loud = [normalizer.process(tone(0.25)) for _ in range(4)]
        self.assertLess(rms(loud[-1]) / rms(quiet[-1]), 2.0)
        self.assertLess(rms(loud[0]) / rms(quiet[-1]), 2.0)
        for chunk in quiet + loud:
            self.assertLessEqual(np.max(np.abs(np.frombuffer(chunk, dtype="<i2"))), 31457)

    def test_silence_does_not_raise_gain(self):
        normalizer = StreamingLoudnessNormalizer()
        normalizer.process(tone(0.2))
        gain_before = normalizer.gain
        silence = bytes(SAMPLE_RATE * 2)
        self.assertEqual(normalizer.process(silence), silence)
        self.assertEqual(normalizer.gain, gain_before)


class PhraseLoudnessTests(unittest.TestCase):
    def test_quiet_and_loud_phrases_have_matching_levels_without_gain_ramps(self):
        # Model phrases may differ by 20 dB. The former fixed gate and
        # amplification cap left the quiet phrase below the loud phrase.
        normalizer = PhraseLoudnessNormalizer()
        for amplitude in (.015, .05, .15):
            source = tone(amplitude, seconds=2)
            output = normalizer.process(source)
            self.assertAlmostEqual(rms(output), .12, delta=.003)
            raw = np.frombuffer(source, dtype='<i2').astype(float)
            adjusted = np.frombuffer(output, dtype='<i2').astype(float)
            self.assertGreater(np.corrcoef(raw, adjusted)[0, 1], .999999)
            self.assertEqual(len(output), len(source))
            self.assertAlmostEqual(rms(output[:48000]), rms(output[-48000:]), delta=.0001)

    def test_pauses_and_noise_do_not_determine_the_speech_level(self):
        normalizer = PhraseLoudnessNormalizer()
        speech = tone(.03)
        paused = bytes(48000) + speech + bytes(96000)
        processed = normalizer.process(paused)
        self.assertAlmostEqual(rms(processed[48000:48000+len(speech)]), .12, delta=.003)
        self.assertEqual(processed[:48000], bytes(48000))
        self.assertEqual(normalizer.process(bytes(48000)), bytes(48000))
        noise = tone(.001)
        self.assertEqual(normalizer.process(noise), noise)

    def test_isolated_peaks_limit_constant_gain_without_clipping(self):
        samples = np.frombuffer(tone(.02), dtype='<i2').copy()
        samples[100] = 15000
        source = samples.tobytes()
        output = PhraseLoudnessNormalizer().process(source)
        adjusted = np.frombuffer(output, dtype='<i2').astype(float)
        self.assertLessEqual(np.max(np.abs(adjusted)), 31458)
        self.assertGreater(np.corrcoef(samples.astype(float), adjusted)[0, 1], .999999)


if __name__ == "__main__":
    unittest.main()
