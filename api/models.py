"""
Pydantic models for API request/response schemas.

Contains all data models used by the speech API endpoints.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator
import base64


class VoiceResponse(BaseModel):
    """Voice metadata response."""
    voice_id: str
    name: str
    languages: list[str]
    language_code: str
    gender: Optional[str] = None
    description: Optional[str] = None


class VoicesResponse(BaseModel):
    """Response for GET /v1/voices endpoint."""
    voices: list[VoiceResponse]


class OpenAISpeechRequest(BaseModel):
    """
    OpenAI-compatible speech request (POST /v1/audio/speech).

    Core fields match the OpenAI TTS API. Extended fields (generation params,
    zero-shot cloning) can be passed via the OpenAI SDK's `extra_body` parameter.
    """
    # --- OpenAI-standard fields ---
    model: str = Field(default="speech", description="Public API alias; the loaded model is selected by server configuration")
    input: str = Field(..., min_length=1, max_length=5000, description="Text to synthesize")
    voice: str = Field(..., description="Voice ID (e.g. 'hi_male', 'en_female') or speaker name (e.g. 'Hindi (Male)')")
    response_format: str = Field(default="mp3", description="Audio format: mp3, opus, aac, wav, pcm")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="Pitch-preserving playback speed; 1.0 preserves the voice's native cadence")

    # --- Extended fields ---
    stream: bool = Field(default=False, description="Stream audio response")
    steady_volume: bool = Field(default=True, description="Smooth loudness changes between generated speech segments")

    # Zero-shot voice cloning
    reference_audio: Optional[bytes] = Field(None, description="Reference audio as base64-encoded string for zero-shot voice cloning. Supports WAV, MP3, FLAC, OGG, etc.")
    reference_transcript: Optional[str] = Field(None, description="Transcript of the reference audio (improves cloning quality)")

    # Generation parameters
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0, description="Sampling temperature (default: 0.75)")
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0, description="Nucleus sampling probability (default: 0.9)")
    top_k: Optional[int] = Field(None, ge=-1, description="Top-k sampling (default: 40)")
    repetition_penalty: Optional[float] = Field(None, ge=1.0, le=2.0, description="Repetition penalty (default: 1.1)")
    max_tokens: Optional[int] = Field(None, ge=1, le=4096, description="Maximum generated tokens per passage (default: up to 4096 for Marathi female; 2048 for other presets)")
    chunk_size: Optional[int] = Field(None, ge=50, le=1000, description="Max characters per generation (default: 1000 for Marathi female; 200 for other presets; also context bounded)")
    buffer_ms: Optional[int] = Field(None, ge=0, le=5000, description="PCM packet size in milliseconds (default: 600, minimum: 100); streaming endpoints decode audio progressively")

    @field_validator('reference_audio', mode='before')
    @classmethod
    def decode_reference_audio(cls, v):
        """Decode base64-encoded audio string to bytes."""
        if v is None:
            return None
        if isinstance(v, bytes):
            return v
        if isinstance(v, str):
            try:
                return base64.b64decode(v)
            except Exception as e:
                raise ValueError(f"Invalid base64 audio data: {str(e)}")
        raise ValueError("reference_audio must be a base64-encoded string")
