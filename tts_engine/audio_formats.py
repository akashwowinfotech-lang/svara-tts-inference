"""Incremental format encoding with unchanged FFmpeg codec defaults."""
from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from typing import AsyncIterator

logger = logging.getLogger(__name__)


async def audio_stream_converter(pcm_stream: AsyncIterator[bytes], format: str,
                                 sample_rate: int = 24000,
                                 channels: int = 1) -> AsyncIterator[bytes]:
    async with aclosing(pcm_stream):
        if format == 'pcm' or format not in ('mp3', 'opus', 'aac', 'wav'):
            if format != 'pcm':
                logger.warning("Unknown format '%s', falling back to PCM", format)
            async for chunk in pcm_stream:
                yield chunk
            return

        cmd = ['ffmpeg', '-nostdin', '-v', 'error',
               '-probesize', '32', '-analyzeduration', '0',
               '-f', 's16le', '-ar', str(sample_rate), '-ac', str(channels), '-i', 'pipe:0']
        # Ogg normally collects a page before flushing. Shorter pages change
        # only container timing, retaining the encoder and its quality defaults.
        if format == 'opus':
            cmd.extend(['-page_duration', '20000'])
        cmd.extend(['-flush_packets', '1', '-f', 'adts' if format == 'aac' else format, 'pipe:1'])
        process = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        async def feed():
            try:
                async for pcm in pcm_stream:
                    if pcm:
                        process.stdin.write(pcm)
                        await process.stdin.drain()
            except BaseException:
                if process.returncode is None:
                    process.kill()
                raise
            finally:
                process.stdin.close()

        writer = asyncio.create_task(feed())
        errors = asyncio.create_task(process.stderr.read())
        try:
            while chunk := await process.stdout.read(4096):
                yield chunk
            await writer
            await process.wait()
            error = await errors
            if process.returncode:
                raise RuntimeError('Audio encoding failed: ' + error.decode(errors='replace')[:200])
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            if process.returncode is None:
                process.kill()
            await process.wait()
            await asyncio.gather(errors, return_exceptions=True)
