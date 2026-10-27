
from __future__ import annotations
import os
from typing import Iterator, AsyncIterator, List, Optional, Literal, Union
import concurrent.futures
import asyncio
import logging
import torch
from .transports import VLLMEmbeddedTransport
from .mapper import SvaraMapper, SvaraFrameParser, extract_custom_token_numbers
from .pacing import continuity_chunks, phrase_chunks, prepare_phrase
from .codec import SNACCodec, get_or_load_tokenizer
from .encoder import svara_text_to_tokens
from .constants import AUDIO_TOKEN_OFFSETS
from .utils import create_speaker_id
from .buffers import AudioBuffer, SyncFuture, crossfade_pcm
from .utils import chunk_text

logger = logging.getLogger(__name__)


def _detect_max_workers(snac_device: Optional[str] = None) -> int:
    """
    Detect recommended max_workers for SNAC decoding.

    When SNAC runs on CPU, scale with available CPU cores.
    When SNAC runs on GPU, limit workers to avoid GPU contention.
    """
    import os

    # If SNAC is on CPU, use half the CPU cores (minimum 2)
    device = snac_device or os.getenv("SNAC_DEVICE", "cpu")
    if device == "cpu":
        cpu_count = os.cpu_count() or 4
        workers = max(2, cpu_count // 2)
        logger.info(f"SNAC on CPU: {cpu_count} cores available, using {workers} workers")
        return workers

    # SNAC on GPU — keep workers limited to avoid contention
    if torch.cuda.is_available():
        try:
            props = torch.cuda.get_device_properties(0)
            vram_gb = props.total_memory / (1024 ** 3)
            logger.info(f"GPU detected: {props.name}, {vram_gb:.1f}GB VRAM, compute {props.major}.{props.minor}")
            if vram_gb >= 16 and props.major >= 8:
                return 4
        except Exception:
            pass
    return 2


class SvaraTTSOrchestrator:
    """
    Sync/Async TTS orchestrator:
    transport -> mapper -> decoder -> PCM int16 chunks.

    Args:
        transport: The VLLMEmbeddedTransport instance.
        model: The model name (for tokenizer lookup).
        speaker_id: The speaker identifier (e.g., "Hindi (Male)", "English (Female)").
                    If not provided, will be constructed from lang_code and gender.
        lang_code: An ISO 639-1 language code (used if speaker_id not provided).
        gender: The gender of the voice (used if speaker_id not provided).
        prebuffer_seconds: The number of seconds to prebuffer before yielding audio.
        concurrent_decode: If True, decode concurrently.
        max_workers: The number of workers to use for decoding (None = auto-detect).
        snac_window_size: SNAC mapper window size in codes. Must be multiple of 7.
                          28 = 4 frames (default, fast TTFB), 56 = 8 frames (fewer
                          decode calls, better throughput). Set via SNAC_WINDOW_SIZE env var.
        device: Device for SNAC decoder (cuda, mps, cpu, or None for auto).
    """
    def __init__(self,
                 transport: VLLMEmbeddedTransport,
                 model: str = "kenpath/svara-tts-v1",
                 speaker_id: Optional[str] = None,
                 lang_code: str = "en",
                 gender: Literal["male", "female"] = "male",
                 prebuffer_seconds: float = 0.5,
                 concurrent_decode: bool = True,
                 max_workers: Optional[int] = None,
                 snac_window_size: Optional[int] = None,
                 device: Optional[str] = None):
        # If speaker_id is provided, use it; otherwise construct from lang_code and gender
        if speaker_id is None:
            self.speaker_id = create_speaker_id(lang_code, gender)
        else:
            self.speaker_id = speaker_id

        self.model_name = model
        self.tokenizer_model = os.getenv("TOKENIZER_MODEL", os.getenv("VLLM_MODEL", "kenpath/svara-tts-v1"))
        self.tokenizer      = get_or_load_tokenizer(self.tokenizer_model)

        self.transport      = transport
        self.codec      = SNACCodec(device)
        self.prebuffer_samples = int(self.codec.sample_rate * prebuffer_seconds)
        self.concurrent_decode = concurrent_decode

        # Auto-detect optimal workers based on SNAC device
        self.max_workers = max_workers if max_workers is not None else _detect_max_workers(device)

        # SNAC window size: configurable via constructor or env var
        if snac_window_size is not None:
            self.snac_window_size = snac_window_size
        else:
            self.snac_window_size = int(os.getenv("SNAC_WINDOW_SIZE", "28"))

        # Long-text chunking config
        # At 24 kHz each seven-code SNAC frame represents 2048 samples
        # (about 85 ms). Bound phrases by text and spoken units so the prompt
        # and complete output fit the language model's context together.
        self.max_chunk_chars = 200
        self.crossfade_ms = 50         # Crossfade overlap between chunks

        logger.info(f"Orchestrator: max_workers={self.max_workers}, "
                     f"snac_window_size={self.snac_window_size}, "
                     f"prebuffer={prebuffer_seconds}s")

    def warmup(self):
        """Run a dummy SNAC decode to trigger torch.compile and warm caches."""
        logger.info("Warming up SNAC decoder...")
        self.codec.decode_window([1] * self.snac_window_size)
        logger.info("SNAC warmup complete")

    # ------------ SYNC path ------------
    def stream(self,
               text: str,
               audio_reference: Optional[List[int]] = None,
               reference_text: Optional[str] = None,
               speaker_id: Optional[str] = None,
               chunk_size: Optional[int] = None,
               buffer_ms: Optional[int] = None,
               **gen_kwargs) -> Iterator[bytes]:
        """Stream the TTS output, automatically chunking long texts.

        For texts longer than chunk_size, splits at sentence boundaries
        and crossfades between chunks for smooth audio stitching.
        Streams audio progressively within each chunk — only holds back
        the last overlap_ms for crossfading with the next chunk.
        """
        max_chars = chunk_size or self.max_chunk_chars
        prebuf = int(self.codec.sample_rate * buffer_ms / 1000) if buffer_ms is not None else None
        chunks = chunk_text(text, max_len=max_chars)

        if len(chunks) <= 1:
            yield from self._stream_one(text, audio_reference=audio_reference, reference_text=reference_text, speaker_id=speaker_id, prebuffer_samples=prebuf, **gen_kwargs)
            return

        logger.info(f"Long text ({len(text)} chars) split into {len(chunks)} chunks")
        overlap_bytes = int(self.codec.sample_rate * self.crossfade_ms / 1000) * 2  # 2 bytes per sample
        prev_tail: Optional[bytes] = None

        for chunk_text_str in chunks:
            is_last_chunk = (chunk_text_str is chunks[-1])
            trailing = bytearray()

            for b in self._stream_one(chunk_text_str, audio_reference=audio_reference, reference_text=reference_text, speaker_id=speaker_id, prebuffer_samples=prebuf, **gen_kwargs):
                trailing.extend(b)

                if prev_tail is not None:
                    head = bytes(trailing[:overlap_bytes]) if len(trailing) >= overlap_bytes else bytes(trailing)
                    if len(head) >= overlap_bytes:
                        blended = crossfade_pcm(prev_tail, head,
                                                overlap_ms=self.crossfade_ms, sample_rate=self.codec.sample_rate)
                        yield blended
                        trailing = bytearray(trailing[overlap_bytes:])
                        prev_tail = None
                    continue

                if len(trailing) > overlap_bytes:
                    to_yield = bytes(trailing[:-overlap_bytes])
                    trailing = bytearray(trailing[-overlap_bytes:])
                    yield to_yield

            if prev_tail is not None:
                if trailing:
                    blended = crossfade_pcm(prev_tail, bytes(trailing),
                                            overlap_ms=self.crossfade_ms, sample_rate=self.codec.sample_rate)
                    yield blended
                else:
                    yield prev_tail
                prev_tail = None
                trailing = bytearray()

            if not is_last_chunk and len(trailing) > overlap_bytes:
                yield bytes(trailing[:-overlap_bytes])
                prev_tail = bytes(trailing[-overlap_bytes:])
            elif not is_last_chunk:
                prev_tail = bytes(trailing) if trailing else None
            else:
                if trailing:
                    yield bytes(trailing)

    def _stream_one(self,
                    text: str,
                    audio_reference: Optional[List[int]] = None,
                    reference_text: Optional[str] = None,
                    speaker_id: Optional[str] = None,
                    prebuffer_samples: Optional[int] = None,
                    **gen_kwargs) -> Iterator[bytes]:

        prompt = svara_text_to_tokens(
            text=text,
            speaker_id=speaker_id or self.speaker_id,
            audio_tokens=audio_reference,
            transcript=reference_text,
            tokenizer=self.tokenizer,
            return_decoded=True
        )

        logger.info(f"Final prompt before inference: {len(prompt)} chars")

        mapper = SvaraMapper(window_size=self.snac_window_size)
        audio_buf = AudioBuffer(prebuffer_samples if prebuffer_samples is not None else self.prebuffer_samples)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) if self.concurrent_decode else None
        pending: List[concurrent.futures.Future] = []

        def decode(win: List[int]) -> bytes:
            return self.codec.decode_window(win)

        def submit(win: List[int]):
            return executor.submit(decode, win) if executor else SyncFuture(decode(win))

        try:
            for token_text in self.transport.stream(prompt, **gen_kwargs):
                for n in extract_custom_token_numbers(token_text):
                    win = mapper.feed_raw(n)
                    if win is not None:
                        pending.append(submit(win))

                    # Yield when we have enough pending
                    while len(pending) > 2:
                        result = audio_buf.process(pending.pop(0).result())
                        if result:
                            yield result

            # Flush remaining
            for fut in pending:
                result = audio_buf.process(fut.result())
                if result:
                    yield result

            # Flush any prebuffered audio that never hit the threshold
            tail = audio_buf.flush()
            if tail:
                yield tail
        finally:
            if executor:
                executor.shutdown(wait=True)

    # ------------ ASYNC path ------------
    async def astream(self,
                      text: str,
                      audio_reference: Optional[List[int]] = None,
                      reference_text: Optional[str] = None,
                      speaker_id: Optional[str] = None,
                      chunk_size: Optional[int] = None,
                      buffer_ms: Optional[int] = None,
                      speed: float = 1.0,
                      steady_volume: bool = True,
                      **gen_kwargs) -> AsyncIterator[bytes]:
        """Stream complete, context-decoded phrases at the requested speed.

        Preparing each phrase before emission prevents a changing startup gain,
        codec-window seams. Normal speed retains the native voice cadence.
        Only one following phrase is prepared ahead of the consumer.
        """
        chunker = continuity_chunks if audio_reference is None else phrase_chunks
        phrases = chunker(text, max_chars=chunk_size or self.max_chunk_chars)
        if not phrases:
            return

        # Preset names describe a voice category, not a unique recording.
        # Keep the opening voice as acoustic conditioning for this request.
        # The bounded reference leaves room for later text and audio tokens.
        voice_anchor: List[int] = []

        async def render(phrase: str) -> bytes:
            reference = audio_reference if audio_reference is not None else (voice_anchor or None)
            pcm = b''.join([part async for part in self._astream_one(
                phrase, audio_reference=reference,
                reference_text=(reference_text if audio_reference is not None else
                                (phrases[0] if voice_anchor else None)),
                capture_reference=(voice_anchor if reference is None and len(phrases) > 1 else None),
                continuation_reference=audio_reference is None and bool(voice_anchor),
                speaker_id=speaker_id, **gen_kwargs)])
            return await prepare_phrase(pcm, phrase, speed, steady_volume)

        pending = asyncio.create_task(render(phrases[0]))
        try:
            for index in range(len(phrases)):
                pcm = await pending
                pending = (asyncio.create_task(render(phrases[index + 1]))
                           if index + 1 < len(phrases) else None)
                # The first packet contains enough playable audio for clients
                # to establish a queue; subsequent packets are contiguous PCM.
                packet_bytes = max(4800, int(24000 * (buffer_ms if buffer_ms is not None else 600) / 1000) * 2)
                for offset in range(0, len(pcm), packet_bytes):
                    yield pcm[offset:offset + packet_bytes]
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def _astream_one(self,
                           text: str,
                           audio_reference: Optional[List[int]] = None,
                           reference_text: Optional[str] = None,
                           speaker_id: Optional[str] = None,
                           prebuffer_samples: Optional[int] = None,
                           capture_reference: Optional[List[int]] = None,
                           continuation_reference: bool = False,
                           **gen_kwargs) -> AsyncIterator[bytes]:
        selected_speaker = speaker_id or self.speaker_id
        if continuation_reference:
            # Match the preset's standard training prompt on both turns.
            # Acoustic conditioning alone can blur initial target words.
            text = f'{selected_speaker}: {text}'
            if reference_text:
                reference_text = f'{selected_speaker}: {reference_text}'
        prompt = svara_text_to_tokens(
            text=text, speaker_id=selected_speaker,
            audio_tokens=audio_reference, transcript=reference_text,
            tokenizer=self.tokenizer, return_decoded=True)
        prompt_tokens = len(self.tokenizer.encode(prompt, add_special_tokens=False))
        available = int(os.getenv('VLLM_MAX_MODEL_LEN', '2048')) - prompt_tokens - 8
        if available < 64:
            raise ValueError('Voice reference and text exceed the model context; use a shorter reference')
        options = {**gen_kwargs, 'max_tokens': min(gen_kwargs.get('max_tokens', 2048), available)}
        parser = SvaraFrameParser()
        async for token_text in self.transport.astream(prompt, **options):
            parser.feed_text(token_text)
        if not parser.codes:
            raise RuntimeError('The speech model returned no complete audio frames')
        if capture_reference is not None:
            # Reuse exact generated codes rather than lossy waveform encoding.
            # Retain the complete short opening and its exact transcript.
            # Cropping mid-word with no transcript can lose initial words in
            # the next phrase. The planner bounds the opening instead.
            capture_reference[:] = [code + AUDIO_TOKEN_OFFSETS[index % 7]
                                    for index, code in enumerate(parser.codes)]
        # Decode once with the complete phrase context. The former sliding
        # crop dropped opening/final frames and introduced seams every 85 ms.
        pcm = await asyncio.to_thread(self.codec.decode_codes, parser.codes)
        if pcm:
            yield pcm
