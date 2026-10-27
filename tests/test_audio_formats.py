"""Check playable early payloads, codec quality and HTTP stream cleanup."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from tts_engine.audio_formats import audio_stream_converter


async def ffmpeg(args, data):
    process = await asyncio.create_subprocess_exec(
        'ffmpeg', '-nostdin', '-v', 'error', *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, error = await asyncio.wait_for(process.communicate(data), 10)
    if process.returncode:
        raise RuntimeError(error.decode())
    return output


class FormatTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_formats_deliver_early_and_retain_existing_encoder_quality(self):
        phase = np.arange(24000 * 4) / 24000
        pcm = (np.sin(2 * np.pi * 220 * phase) * 12000).astype('<i2').tobytes()
        for format in ('pcm', 'wav', 'mp3', 'aac', 'opus'):
            gate = asyncio.Event()
            closed = asyncio.Event()

            async def source():
                try:
                    yield pcm[:96000]
                    await gate.wait()
                    yield pcm[96000:]
                finally:
                    closed.set()

            output = audio_stream_converter(source(), format)
            parts = []
            async with asyncio.timeout(5):
                while sum(map(len, parts)) < 512:
                    parts.append(await anext(output))
            self.assertFalse(gate.is_set(), format)
            gate.set()
            parts.extend([part async for part in output])
            self.assertTrue(closed.is_set(), format)
            encoded = b''.join(parts)
            if format == 'pcm':
                self.assertEqual(encoded, pcm)
                continue
            # Compare actual decoded samples, excluding random container IDs
            # and page layout. Input probing/flushing must not lower quality.
            old = await ffmpeg(['-f', 's16le', '-ar', '24000', '-ac', '1', '-i', 'pipe:0',
                                '-f', 'adts' if format == 'aac' else format, 'pipe:1'], pcm)
            decode = ['-i', 'pipe:0', '-f', 's16le', '-ac', '1', '-ar', '24000', 'pipe:1']
            a = await ffmpeg(decode, old)
            b = await ffmpeg(decode, encoded)
            self.assertEqual(a, b, format)

    async def test_encoder_cancel_and_error_close_generation(self):
        for format in ('pcm', 'mp3', 'opus'):
            closed = asyncio.Event()

            async def source():
                try:
                    yield bytes(96000)
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            encoded = audio_stream_converter(source(), format)
            await asyncio.wait_for(anext(encoded), 5)
            await asyncio.wait_for(encoded.aclose(), 5)
            self.assertTrue(closed.is_set(), format)

            async def failing():
                yield bytes(96000)
                raise ValueError('synthesis failed')

            with self.assertRaisesRegex(ValueError, 'synthesis failed'):
                [part async for part in audio_stream_converter(failing(), format)]

    async def test_http_streaming_selects_progressive_mode_and_closes_metered_source(self):
        from api.models import OpenAISpeechRequest
        from api.server import build_audio_stream

        for stream in (False, True):
            closed = asyncio.Event()

            async def source():
                try:
                    yield bytes(4800)
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            def generate(**kwargs):
                self.assertEqual(kwargs['progressive'], stream)
                return source()

            with patch('api.server.orchestrator', SimpleNamespace(astream=generate)):
                usage = {'seconds': 0.}
                output, media = await build_audio_stream(OpenAISpeechRequest(
                    input='नमस्कार.', voice='mr_female', stream=stream, response_format='pcm'), usage)
                self.assertEqual(media, 'audio/pcm')
                self.assertEqual(await anext(output), bytes(4800))
                self.assertAlmostEqual(usage['seconds'], .1)
                await output.aclose()
                self.assertTrue(closed.is_set())


if __name__ == '__main__':
    unittest.main()
