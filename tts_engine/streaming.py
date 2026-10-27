"""Progressive PCM delivery without restarting the speech model mid-phrase."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import AsyncIterator, Callable

import numpy as np


class StreamingSNACDecoder:
    """Decode bounded windows, retaining aligned left and future context.

    The 24 kHz codec has seven codes and 2048 samples per coarse frame.
    Eight frames on either side cover its convolution context. Unlike the
    former tiny sliding crop, this keeps every opening and ending sample.
    """

    samples_per_frame = 2048
    context_frames = 8
    step_frames = 16
    first_frames = 24

    def __init__(self, codec, on_initial_pcm: Callable[[bytes], None] | None = None):
        self.codec = codec
        self.on_initial_pcm = on_initial_pcm
        self.emitted = 0
        self.previous: tuple[int, bytes] | None = None

    def ready(self, frames: int) -> bool:
        threshold = (self.first_frames if self.emitted == 0 else
                     self.emitted + self.step_frames + self.context_frames)
        return frames >= threshold

    def decode(self, codes: list[int], final: bool = False) -> bytes:
        frames = len(codes) // 7
        if frames <= self.emitted or (not final and not self.ready(frames)):
            return b''
        end = (frames if final else
               self.first_frames - self.context_frames if not self.emitted else
               self.emitted + self.step_frames)
        lo = max(0, self.emitted - self.context_frames)
        hi = frames if final else end + self.context_frames
        pcm = self.codec.decode_codes(codes[lo * 7:hi * 7])
        frame_bytes = self.samples_per_frame * 2
        if len(pcm) != (hi - lo) * frame_bytes:
            raise RuntimeError('Unexpected audio codec frame length')
        if not self.emitted and self.on_initial_pcm is not None:
            # Use the already decoded future context for the same 2.048 s
            # volume estimate, without waiting for eight more model frames.
            self.on_initial_pcm(pcm[:24 * frame_bytes])
        offset = (self.emitted - lo) * frame_bytes
        piece = pcm[offset:(end - lo) * frame_bytes]
        if self.previous is not None:
            # The codec adds stochastic breath noise on each decode. Blend
            # the same absolute samples from the previous lookahead window;
            # this neither removes overlap nor repeats speech at the join.
            previous_lo, previous_pcm = self.previous
            previous_offset = (self.emitted - previous_lo) * frame_bytes
            count = min(480, len(piece) // 2,
                        (len(previous_pcm) - previous_offset) // 2)
            if count > 0:
                old = np.frombuffer(previous_pcm, '<i2', count, previous_offset).astype(float)
                new = np.frombuffer(piece, '<i2', count).astype(float)
                weight = np.linspace(0, 1, count)
                head = np.rint(old * (1 - weight) + new * weight).astype('<i2').tobytes()
                piece = head + piece[count * 2:]
        self.previous = (lo, pcm)
        self.emitted = end
        return piece


class FixedStreamingGain:
    """Choose gain from the initial speech, then keep it fixed for the phrase.

    No packet AGC or gain ramp is used. A static, smooth peak knee protects
    unusually loud later transients without changing the gain over time.
    """

    def __init__(self):
        self.gain: float | None = None

    def calibrate(self, pcm: bytes) -> None:
        if self.gain is not None:
            return
        samples = np.frombuffer(pcm, '<i2').astype(float) / 32768
        if not samples.size:
            return
        size = samples.size // 480
        levels = (np.sqrt(np.mean(samples[:size * 480].reshape(-1, 480) ** 2, axis=1))
                  if size else np.array([np.sqrt(np.mean(samples ** 2))]))
        gate = max(.0015, float(np.percentile(levels, 95)) * .1)
        active = levels[levels >= gate]
        if active.size:
            self.gain = min(16., .12 / float(np.median(active)), .90 / np.max(np.abs(samples)))

    def process(self, pcm: bytes) -> bytes:
        self.calibrate(pcm)
        if self.gain is None:
            return pcm
        samples = np.frombuffer(pcm, '<i2').astype(float) / 32768
        output = samples * self.gain
        magnitude = np.abs(output)
        loud = magnitude > .88
        output[loud] = np.sign(output[loud]) * (.88 + .08 * -np.expm1(-(magnitude[loud] - .88) / .08))
        return np.rint(output * 32768).astype('<i2').tobytes()


async def prepare_stream(source: AsyncIterator[bytes], speed: float,
                         steady_volume: bool,
                         normalizer: FixedStreamingGain | None = None) -> AsyncIterator[bytes]:
    """Use one gain and, if requested, one continuous tempo filter per phrase."""
    gain = (normalizer or FixedStreamingGain()) if steady_volume else None
    async with aclosing(source):
        if abs(speed - 1.) <= 1e-8:
            async for pcm in source:
                if pcm:
                    yield gain.process(pcm) if gain else pcm
            return

        # atempo preserves pitch. Keeping one process for the entire phrase
        # avoids restarting its analysis buffer at every network packet.
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-nostdin', '-v', 'error', '-probesize', '32', '-analyzeduration', '0',
            '-f', 's16le', '-ar', '24000', '-ac', '1', '-i', 'pipe:0',
            '-af', f'atempo={speed:.8f}', '-f', 's16le', '-flush_packets', '1', 'pipe:1',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        async def feed():
            try:
                async for pcm in source:
                    if pcm:
                        process.stdin.write(gain.process(pcm) if gain else pcm)
                        await process.stdin.drain()
            except BaseException:
                if process.returncode is None:
                    process.kill()
                raise
            finally:
                process.stdin.close()

        writer = asyncio.create_task(feed())
        errors = asyncio.create_task(process.stderr.read())
        pending = b''
        try:
            while chunk := await process.stdout.read(8192):
                # Pipe reads need not end on a PCM16 sample boundary.
                pending += chunk
                size = len(pending) // 2 * 2
                if size:
                    yield pending[:size]
                    pending = pending[size:]
            await writer
            await process.wait()
            error = await errors
            if process.returncode or pending:
                raise RuntimeError('Speech tempo processing failed: ' + error.decode(errors='replace')[:200])
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            if process.returncode is None:
                process.kill()
            await process.wait()
            await asyncio.gather(errors, return_exceptions=True)
