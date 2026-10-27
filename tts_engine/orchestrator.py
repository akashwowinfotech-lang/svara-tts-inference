
from __future__ import annotations
import os
from typing import Iterator, AsyncIterator, List, Optional, Literal, Union, Callable
import concurrent.futures
import asyncio
import logging
from contextlib import aclosing
import torch
from .transports import VLLMEmbeddedTransport
from .mapper import SvaraMapper, SvaraFrameParser, extract_custom_token_numbers
from .pacing import phrase_chunks, prepare_phrase
from .streaming import StreamingSNACDecoder, FixedStreamingGain, prepare_stream
from .codec import SNACCodec, get_or_load_tokenizer
from .encoder import svara_text_to_tokens
from .utils import create_speaker_id
from .buffers import AudioBuffer, SyncFuture, crossfade_pcm
from .utils import chunk_text

logger = logging.getLogger(__name__)

# Roll out the larger default only where listening feedback accepted it.
# Other presets retain their previous defaults after Hindi male looped in
# the expanded-paragraph check. Explicit larger-chunk options remain usable.
PARAGRAPH_SPEAKERS = frozenset({'Marathi (Female)'})


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
        self.max_paragraph_chars = 1000
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
                      progressive: bool = False,
                      **gen_kwargs) -> AsyncIterator[bytes]:
        """Stream context-decoded phrases at the requested speed.

        Streaming endpoints opt into decoding with bounded codec context.
        Other callers prepare full phrases with one following phrase prefetched.
        Both retain one model generation per phrase and native cadence at 1x.
        """
        native_preset = audio_reference is None
        selected_speaker = speaker_id or getattr(self, 'speaker_id', '')
        approved_paragraph = native_preset and selected_speaker in PARAGRAPH_SPEAKERS
        default_chars = (getattr(self, 'max_paragraph_chars', self.max_chunk_chars)
                         if approved_paragraph else self.max_chunk_chars)
        chars = chunk_size or default_chars
        unit_limit = 60
        if native_preset and chars > 200:
            # Keep fitting paragraphs in one native generation for every
            # preset. Leave room for text and special tokens as well.
            context = int(os.getenv('VLLM_MAX_MODEL_LEN', '4096'))
            unit_limit = min(160, max(60, (context - 512) // 22))
        phrases = phrase_chunks(text, max_chars=chars, max_units=unit_limit)
        if not phrases:
            return

        packet_bytes = max(4800, int(24000 * (buffer_ms if buffer_ms is not None else 600) / 1000) * 2)
        if progressive:
            # Faster playback consumes audio sooner than native generation.
            # Reserve up to 2.4 output seconds at 2x, once per request, rather
            # than letting the client repeatedly run out between windows.
            initial_bytes = max(packet_bytes, int(48000 * 2.4 * max(0., speed - 1.)))
            playback_started = False
            for phrase in phrases:
                gain = FixedStreamingGain() if steady_volume else None
                source = self._astream_one(
                    phrase, audio_reference=audio_reference, reference_text=reference_text,
                    speaker_id=speaker_id, progressive=True,
                    on_initial_pcm=gain.calibrate if gain else None, **gen_kwargs)
                prepared = prepare_stream(source, speed, steady_volume, normalizer=gain)
                async with aclosing(prepared):
                    pending_pcm = bytearray()
                    async for pcm in prepared:
                        pending_pcm.extend(pcm)
                        if not playback_started and len(pending_pcm) < initial_bytes:
                            continue
                        playback_started = True
                        while len(pending_pcm) >= packet_bytes:
                            yield bytes(pending_pcm[:packet_bytes])
                            del pending_pcm[:packet_bytes]
                    if pending_pcm:
                        playback_started = True
                        yield bytes(pending_pcm)
            return

        async def render(phrase: str) -> bytes:
            pcm = b''.join([part async for part in self._astream_one(
                phrase, audio_reference=audio_reference, reference_text=reference_text,
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
                           progressive: bool = False,
                           on_initial_pcm: Optional[Callable[[bytes], None]] = None,
                           **gen_kwargs) -> AsyncIterator[bytes]:
        selected_speaker = speaker_id or self.speaker_id
        prompt = svara_text_to_tokens(
            text=text, speaker_id=selected_speaker,
            audio_tokens=audio_reference, transcript=reference_text,
            tokenizer=self.tokenizer, return_decoded=True)
        prompt_tokens = len(self.tokenizer.encode(prompt, add_special_tokens=False))
        available = int(os.getenv('VLLM_MAX_MODEL_LEN', '4096')) - prompt_tokens - 8
        if available < 64:
            raise ValueError('Voice reference and text exceed the model context; use a shorter reference')
        default_tokens = (4096 if audio_reference is None and selected_speaker in PARAGRAPH_SPEAKERS
                          else 2048)
        options = {**gen_kwargs, 'max_tokens': min(gen_kwargs.get('max_tokens', default_tokens), available)}
        parser = SvaraFrameParser()
        decoder = StreamingSNACDecoder(self.codec, on_initial_pcm) if progressive else None
        source = self.transport.astream(prompt, **options)
        async with aclosing(source):
            async for token_text in source:
                parser.feed_text(token_text)
                if decoder is not None:
                    while decoder.ready(len(parser.codes) // 7):
                        yield await asyncio.to_thread(decoder.decode, list(parser.codes))
        if not parser.codes:
            raise RuntimeError('The speech model returned no complete audio frames')
        if decoder is not None:
            tail = await asyncio.to_thread(decoder.decode, list(parser.codes), True)
            if tail:
                yield tail
            return
        # Decode once with the complete phrase context. The former sliding
        # crop dropped opening/final frames and introduced seams every 85 ms.
        pcm = await asyncio.to_thread(self.codec.decode_codes, parser.codes)
        if pcm:
            yield pcm
