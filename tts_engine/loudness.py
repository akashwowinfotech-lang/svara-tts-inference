"""Gentle, per-request loudness control for streaming 24 kHz mono PCM16."""

from __future__ import annotations

import math
from typing import AsyncIterator

import numpy as np


class PhraseLoudnessNormalizer:
    """Apply one gain to a complete phrase, retaining its voice and dynamics.

    A relative speech gate can measure quiet synthesized voices as well as
    loud ones. Unlike packet-level AGC, this never ramps the gain while a
    phrase is playing. Peak headroom limits amplification without clipping.
    """

    def __init__(self, sample_rate: int = 24000, target_rms: float = 0.12):
        self.sample_rate = sample_rate
        self.target_rms = target_rms

    def process(self, pcm: bytes) -> bytes:
        if not pcm or len(pcm) % 2:
            return pcm
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / 32768.0
        frame_size = max(1, self.sample_rate // 50)
        complete = samples.size // frame_size
        if complete:
            frames = samples[:complete * frame_size].reshape(-1, frame_size)
            levels = np.sqrt(np.mean(frames * frames, axis=1))
        else:
            levels = np.array([np.sqrt(np.mean(samples * samples))])
        # The old absolute 0.025 gate ignored much of a quiet phrase, and
        # its 3.5x amplification cap left that phrase far below the next.
        gate = max(0.0015, float(np.percentile(levels, 95)) * 0.1)
        active = levels[levels >= gate]
        if not active.size:
            return pcm
        peak = float(np.max(np.abs(samples)))
        gain = min(16.0, self.target_rms / float(np.median(active)), 0.96 / peak)
        return np.rint(samples * gain * 32768.0).astype('<i2').tobytes()


class StreamingLoudnessNormalizer:
    """Reduce level jumps between synthesis segments without flattening speech.

    A gate excludes silence and quiet breaths from the level estimate. The
    speech-level estimate and gain move smoothly across calls. Each request
    creates its own instance, so concurrent speakers never share gain state.
    """

    def __init__(self, sample_rate: int = 24000, target_rms: float = 0.16):
        self.sample_rate = sample_rate
        self.target_rms = target_rms
        self.level: float | None = None
        self.gain = 1.0

    def process(self, pcm: bytes) -> bytes:
        if not pcm or len(pcm) % 2:
            return pcm

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if not samples.size:
            return pcm

        # The median of active 10 ms frames follows the voice, not pauses.
        frame_size = max(1, self.sample_rate // 100)
        active_levels = []
        for start in range(0, samples.size, frame_size):
            frame = samples[start : start + frame_size]
            rms = float(np.sqrt(np.mean(frame * frame)))
            if rms >= 0.025:
                active_levels.append(rms)

        duration = samples.size / self.sample_rate
        first_speech = self.level is None and bool(active_levels)
        sudden_increase = False
        if active_levels:
            measured = float(np.median(active_levels))
            if self.level is None:
                self.level = measured
            elif measured > self.level * 1.5:
                # A newly generated segment can begin much louder. React
                # before sending most of that segment to the listener.
                self.level = measured
                sudden_increase = True
            else:
                tau = 0.30 if measured > self.level else 0.40
                alpha = 1.0 - math.exp(-duration / tau)
                self.level += alpha * (measured - self.level)

        desired = self.gain if self.level is None else self.target_rms / self.level
        desired = min(3.5, max(0.45, desired))
        previous = self.gain
        if first_speech or sudden_increase:
            updated = desired
        else:
            tau = 0.15 if desired < previous else 0.35
            alpha = 1.0 - math.exp(-duration / tau)
            updated = previous + alpha * (desired - previous)

        peak = float(np.max(np.abs(samples)))
        if peak:
            updated = min(updated, 0.96 / peak)
        self.gain = updated

        if first_speech:
            envelope = np.full(samples.size, updated, dtype=np.float32)
        elif sudden_increase:
            attack = min(samples.size, max(1, self.sample_rate // 50))
            envelope = np.full(samples.size, updated, dtype=np.float32)
            envelope[:attack] = np.linspace(previous, updated, attack, dtype=np.float32)
        else:
            envelope = np.linspace(previous, updated, samples.size, dtype=np.float32)
        output = np.clip(samples * envelope, -0.96, 0.96)
        return (output * 32767.0).astype("<i2").tobytes()


async def normalize_pcm_stream(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    normalizer = StreamingLoudnessNormalizer()
    async for chunk in source:
        if chunk:
            yield normalizer.process(chunk)
