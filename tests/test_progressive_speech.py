"""Check early delivery, contiguous codec samples, tempo and cancellation."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import numpy as np

from tts_engine.streaming import StreamingSNACDecoder, FixedStreamingGain, prepare_stream


class IndexedCodec:
    def __init__(self):
        self.windows = []

    def decode_codes(self, codes):
        self.windows.append(len(codes) // 7)
        # Absolute frame IDs make any lost/duplicated samples observable.
        return np.repeat(np.array(codes[::7], dtype='<i2'), 2048).tobytes()


class CodecTests(unittest.TestCase):
    def test_windows_retain_every_frame_and_bound_decode_work(self):
        for total in (1, 8, 20, 23, 24, 25, 31, 32, 40, 48, 63, 64, 349):
            codec = IndexedCodec()
            decoder = StreamingSNACDecoder(codec)
            codes, output = [], []
            for index in range(total):
                codes.extend([index] * 7)
                while decoder.ready(len(codes) // 7):
                    output.append(decoder.decode(codes))
            output.append(decoder.decode(codes, final=True))
            expected = np.repeat(np.arange(total, dtype='<i2'), 2048).tobytes()
            self.assertEqual(b''.join(output), expected, total)
            self.assertLessEqual(max(codec.windows), 32)
            self.assertEqual(decoder.decode(codes, final=True), b'')

    def test_initial_volume_calibration_retains_two_seconds_with_earlier_audio(self):
        calibration = []
        decoder = StreamingSNACDecoder(IndexedCodec(), calibration.append)
        codes = [frame for frame in range(24) for band in range(7)]
        piece = decoder.decode(codes)
        self.assertEqual(len(piece), 16 * 2048 * 2)
        self.assertEqual(len(calibration), 1)
        self.assertEqual(len(calibration[0]), 24 * 2048 * 2)
        decoder.decode(codes + [24] * 7, final=True)
        self.assertEqual(len(calibration), 1)

    def test_fixed_gain_has_no_packet_ramp_and_handles_later_peaks(self):
        normalizer = FixedStreamingGain()
        phase = np.arange(48000) / 24000
        pcm = (np.sin(2 * np.pi * 220 * phase) * 2000).astype('<i2').tobytes()
        self.assertEqual(normalizer.process(bytes(48000)), bytes(48000))
        self.assertIsNone(normalizer.gain)
        a = normalizer.process(pcm)
        gain = normalizer.gain
        self.assertEqual(normalizer.process(pcm), a)
        loud = normalizer.process((np.sin(2 * np.pi * 220 * phase) * 30000).astype('<i2').tobytes())
        self.assertEqual(normalizer.gain, gain)
        self.assertLess(np.max(np.abs(np.frombuffer(loud, '<i2').astype(float))), .961 * 32768)
        self.assertGreater(np.corrcoef(np.frombuffer(a, '<i2'), np.frombuffer(pcm, '<i2'))[0, 1], .999999)


class ProgressiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_aborts_closed_generation_but_not_completed_generation(self):
        from tts_engine.transports import VLLMEmbeddedTransport

        class Engine:
            def __init__(self):
                self.abort = AsyncMock()

            async def generate(self, *args):
                yield SimpleNamespace(outputs=[SimpleNamespace(text='first')])
                yield SimpleNamespace(outputs=[SimpleNamespace(text='first second')])

        engine = Engine()
        transport = VLLMEmbeddedTransport('test')
        transport._engine = engine
        with patch.dict('sys.modules', {'vllm.sampling_params': SimpleNamespace(SamplingParams=lambda **kw: kw)}):
            stream = transport.astream('prompt')
            self.assertEqual(await anext(stream), 'first')
            await stream.aclose()
            engine.abort.assert_awaited_once()
            engine.abort.reset_mock()
            self.assertEqual([part async for part in transport.astream('prompt')], ['first', ' second'])
            engine.abort.assert_not_awaited()

    async def test_first_pcm_precedes_generation_completion_and_close_aborts_source(self):
        from tts_engine.orchestrator import SvaraTTSOrchestrator

        class Transport:
            calls = 0
            closed = False

            async def astream(self, prompt, **options):
                self.calls += 1
                try:
                    yield ''.join(f'<custom_token_{10 + band * 4096}>'
                                  for frame in range(24) for band in range(7))
                    await asyncio.Event().wait()  # Generation has not finished.
                finally:
                    self.closed = True

        core = SvaraTTSOrchestrator.__new__(SvaraTTSOrchestrator)
        core.speaker_id = 'Marathi (Female)'
        core.tokenizer = SimpleNamespace(encode=lambda *args, **kwargs: [1] * 40)
        core.transport = Transport()
        core.codec = IndexedCodec()
        core.max_chunk_chars = 200
        core.max_paragraph_chars = 1000
        with patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt'):
            source = core.astream('निसर्ग हा माणसाचा खरा मित्र आहे.',
                                  progressive=True, steady_volume=False)
            first = await asyncio.wait_for(anext(source), 5)
            self.assertEqual(len(first), 28800)
            self.assertFalse(core.transport.closed)
            await source.aclose()
        self.assertTrue(core.transport.closed)
        self.assertEqual(core.transport.calls, 1)

    async def test_progressive_tempo_outputs_before_source_finishes_and_keeps_pitch(self):
        phase = np.arange(24000 * 6) / 24000
        pcm = (np.sin(2 * np.pi * 220 * phase) * 10000).astype('<i2').tobytes()
        for speed in (.5, 1., 2.):
            gate = asyncio.Event()
            closed = False

            async def source():
                nonlocal closed
                try:
                    yield pcm[:192000]  # Four seconds, then wait for early output.
                    await gate.wait()
                    yield pcm[192000:]
                finally:
                    closed = True

            prepared = prepare_stream(source(), speed, False)
            first = await asyncio.wait_for(anext(prepared), 5)
            self.assertFalse(gate.is_set())
            gate.set()
            output = first + b''.join([part async for part in prepared])
            self.assertTrue(closed)
            self.assertAlmostEqual(len(output) / 48000, 6 / speed, delta=.1)
            samples = np.frombuffer(output, '<i2').astype(float)
            spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
            self.assertAlmostEqual(np.argmax(spectrum) * 24000 / len(samples), 220, delta=2)
            if speed == 1.:
                self.assertEqual(output, pcm)

    async def test_tempo_close_cancels_writer_and_closes_source(self):
        closed = asyncio.Event()

        async def source():
            try:
                yield bytes(192000)
                await asyncio.Event().wait()
            finally:
                closed.set()

        prepared = prepare_stream(source(), 1.5, False)
        await asyncio.wait_for(anext(prepared), 5)
        await asyncio.wait_for(prepared.aclose(), 5)
        self.assertTrue(closed.is_set())

    async def test_tempo_propagates_generation_failure(self):
        async def source():
            yield bytes(192000)
            raise ValueError('generation failed')

        with self.assertRaisesRegex(ValueError, 'generation failed'):
            [part async for part in prepare_stream(source(), 1.5, False)]


if __name__ == '__main__':
    unittest.main()
