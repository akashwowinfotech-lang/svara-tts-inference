"""Detect when a separately generated phrase comes out in a different voice.

The model draws a new voice for every generation and now and then the draw is
clearly different from the earlier phrases of the same request. This compares
the average spectrum of a phrase's opening seconds with the voice of the
request's first phrase. It is a cheap numpy computation: no model, and it
never changes the audio, it only tells the caller to generate that phrase again.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

SAMPLE_RATE = 24000
N_FFT = 1024
HOP = 480
N_MELS = 48
CHECK_FRAMES = 36      # SNAC frames (about 3 s) heard before a phrase is judged
REFERENCE_SECONDS = 9  # opening of the first phrase that defines the voice


def _mel_filters() -> np.ndarray:
    def to_mel(hz):
        return 2595. * np.log10(1. + np.asarray(hz, dtype=float) / 700.)

    def to_hz(mel):
        return 700. * (10. ** (np.asarray(mel, dtype=float) / 2595.) - 1.)

    points = to_hz(np.linspace(to_mel(60.), to_mel(8000.), N_MELS + 2))
    freqs = np.linspace(0., SAMPLE_RATE / 2, N_FFT // 2 + 1)
    filters = np.zeros((N_MELS, freqs.size))
    for i in range(N_MELS):
        lo, mid, hi = points[i:i + 3]
        filters[i] = np.maximum(0., np.minimum((freqs - lo) / (mid - lo), (hi - freqs) / (hi - mid)))
    return filters


# Inverse covariance of how a voice's profile varies between sentences of the
# same voice (words change the spectrum, the speaker should not). It makes
# the distance ignore ordinary variation and react to a different voice.
# Derived by quality-checks/2026-11-25-voice-continuity/tools/make_voice_weights.py.
_WEIGHTS = np.load(Path(__file__).with_name('voice_match_weights.npy'), allow_pickle=False).astype(np.float64)
_FILTERS = _mel_filters()
_WINDOW = np.hanning(N_FFT + 1)[:-1]


def voice_profile(pcm: bytes) -> np.ndarray | None:
    """Average log-mel spectrum (dB) of the speech frames, with loudness removed."""
    samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], '<i2').astype(np.float32) / 32768.
    if samples.size < N_FFT * 2:
        return None
    padded = np.pad(samples, N_FFT // 2, mode='reflect')
    count = 1 + (padded.size - N_FFT) // HOP
    index = np.arange(N_FFT)[None, :] + HOP * np.arange(count)[:, None]
    power = np.abs(np.fft.rfft(padded[index] * _WINDOW, axis=1)) ** 2
    decibels = 10. * np.log10(power @ _FILTERS.T + 1e-10)      # (frames, mels)
    level = decibels.mean(1)
    speech = level > level.max() - 30.
    if speech.sum() < 20:
        return None
    profile = decibels[speech].mean(0)
    return profile - profile.mean()


class VoiceMatcher:
    """Judge phrases of one request against the voice of its first phrase."""

    def __init__(self, threshold: float = 1.2, retries: int = 2):
        self.threshold = threshold
        self.retries = retries
        self.reference: np.ndarray | None = None
        self.replace = False  # set when no attempt matched; the next accepted phrase becomes the reference

    def distance(self, pcm: bytes) -> float | None:
        profile = voice_profile(pcm)
        if profile is None or self.reference is None:
            return None
        delta = profile - self.reference
        return float(np.sqrt(max(0., delta @ _WEIGHTS @ delta) / delta.size))

    def matches(self, pcm: bytes) -> bool:
        distance = self.distance(pcm)
        return distance is None or distance <= self.threshold

    def adopt(self, pcm: bytes, only_if_unset: bool = False) -> None:
        """Take this audio as the request's voice (its first phrase, or a new
        voice after every retry failed to match the old one)."""
        if only_if_unset and self.reference is not None:
            return
        profile = voice_profile(pcm[:REFERENCE_SECONDS * SAMPLE_RATE * 2])
        if profile is not None:
            self.reference = profile
