"""
FastAPI server for the speech API.

Provides OpenAI-compatible text-to-speech endpoints with support for
Indian language voices and streaming audio generation.
"""
from __future__ import annotations
import os
import sys
import logging
from pathlib import Path
from typing import Optional, AsyncGenerator
from contextlib import asynccontextmanager
import asyncio
import json
import time
import ipaddress
import secrets
from datetime import datetime
from urllib.parse import urlsplit
from typing import Literal

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure logging from LOG_LEVEL env var
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="[%(asctime)s] %(levelname)s %(filename)s:%(lineno)d: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Hub clients may otherwise log the internal model URL at INFO during startup.
logging.getLogger("httpx").setLevel(logging.WARNING)

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError


logger = logging.getLogger(__name__)

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tts_engine.voice_config import get_all_voices, get_speaker_id, get_voice
from tts_engine.orchestrator import SvaraTTSOrchestrator
from tts_engine.transports import VLLMEmbeddedTransport
from tts_engine.utils import load_audio_from_bytes
from tts_engine.codec import SNACCodec
from tts_engine.loudness import normalize_pcm_stream
from api.models import VoiceResponse, VoicesResponse, OpenAISpeechRequest
from api.access import AccessDenied, AccessStore, ClientKey, RequestLimiter, client_ip
from api.admin_auth import AdminAuth, COOKIE_NAME, SESSION_HOURS
from api.dashboard_validation import (KeyCreate, KeyUpdate, AllowlistUpdate, AdminLoginRequest,
    admin_validation_error)
from fastapi.exceptions import RequestValidationError


# ============================================================================
# Configuration
# ============================================================================

VLLM_MODEL = os.getenv("VLLM_MODEL", "kenpath/svara-tts-v1")
SNAC_DEVICE = os.getenv("SNAC_DEVICE", None)  # None = auto-detect (CUDA/MPS/CPU). Device for SNAC audio decoder.
VLLM_GPU_MEMORY_UTILIZATION = float(os.getenv("VLLM_GPU_MEMORY_UTILIZATION", "0.9"))
VLLM_MAX_MODEL_LEN = int(os.getenv("VLLM_MAX_MODEL_LEN", "4096"))
VLLM_TENSOR_PARALLEL_SIZE = int(os.getenv("VLLM_TENSOR_PARALLEL_SIZE", "1"))
VLLM_QUANTIZATION = os.getenv("VLLM_QUANTIZATION") or None
VLLM_ENFORCE_EAGER = os.getenv("VLLM_ENFORCE_EAGER", "false").lower() in ("true", "1", "yes")
VLLM_DTYPE = os.getenv("VLLM_DTYPE", "auto")
VLLM_ATTENTION_BACKEND = os.getenv("VLLM_ATTENTION_BACKEND") or None
VLLM_KV_CACHE_DTYPE = os.getenv("VLLM_KV_CACHE_DTYPE", "auto")
# HF_TOKEN is checked in codec.get_or_load_tokenizer() for private models

# Global instances (initialized in lifespan)
orchestrator: Optional[SvaraTTSOrchestrator] = None


# ============================================================================
# Application Lifecycle
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup resources."""
    global orchestrator

    admin_secret = os.getenv("ADMIN_API_KEY", "")
    if not admin_secret:
        secret_file = Path(os.getenv("ADMIN_API_KEY_FILE", ".secrets/admin.key"))
        if not secret_file.is_file():
            raise RuntimeError("Admin key is missing. Start with run-local.sh or set ADMIN_API_KEY_FILE.")
        admin_secret = secret_file.read_text().strip()
    app.state.access = AccessStore(os.getenv("ACCESS_DB_PATH", ".data/access.sqlite3"), admin_secret)
    app.state.admin_auth = AdminAuth(os.getenv("ACCESS_DB_PATH", ".data/access.sqlite3"),
                                     os.getenv("ADMIN_LOGIN_FILE", ".secrets/admin-login.json"))
    app.state.limiter = RequestLimiter(int(os.getenv("MAX_CONCURRENT_SPEECH", "4")))
    
    logger.info("Initializing speech API...")
    logger.info(f"  vLLM:  dtype={VLLM_DTYPE}, quantization={VLLM_QUANTIZATION or 'none'}")
    logger.info(f"  vLLM:  max_model_len={VLLM_MAX_MODEL_LEN}, gpu_mem={VLLM_GPU_MEMORY_UTILIZATION}, tp={VLLM_TENSOR_PARALLEL_SIZE}, enforce_eager={VLLM_ENFORCE_EAGER}")
    logger.info(f"  SNAC:  device={SNAC_DEVICE or 'auto-detect'}, window_size={os.getenv('SNAC_WINDOW_SIZE', '28')}")
    logger.info(f"  API:   host={os.getenv('API_HOST', '0.0.0.0')}, port={os.getenv('API_PORT', '8080')}, log_level={os.getenv('LOG_LEVEL', 'INFO')}")
    logger.info(f"  Auth:  HF_TOKEN={'set' if os.getenv('HF_TOKEN') else 'not set'}")

    # Initialize embedded vLLM engine (singleton)
    VLLMEmbeddedTransport.initialize_engine(
        model=VLLM_MODEL,
        gpu_memory_utilization=VLLM_GPU_MEMORY_UTILIZATION,
        max_model_len=VLLM_MAX_MODEL_LEN,
        tensor_parallel_size=VLLM_TENSOR_PARALLEL_SIZE,
        dtype=VLLM_DTYPE,
        quantization=VLLM_QUANTIZATION,
        enforce_eager=VLLM_ENFORCE_EAGER,
        attention_backend=VLLM_ATTENTION_BACKEND,
        kv_cache_dtype=VLLM_KV_CACHE_DTYPE,
    )
    logger.info("vLLM engine initialized (embedded)")

    transport = VLLMEmbeddedTransport(model=VLLM_MODEL)

    # Initialize orchestrator with default settings
    orchestrator = SvaraTTSOrchestrator(
        transport=transport,
        model=VLLM_MODEL,
        speaker_id="English (Male)",  # Default, will be overridden per request
        device=SNAC_DEVICE,
        prebuffer_seconds=0.5,
        concurrent_decode=True,
    )

    logger.info(f"Orchestrator initialized (workers={orchestrator.max_workers}, prebuffer={0.5}s, chunk_size={orchestrator.max_chunk_chars})")
    logger.info(f"Loaded {len(get_all_voices())} voices")

    orchestrator.warmup()
    
    yield
    
    logger.info("Shutting down speech API...")


# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="Speech API",
    description="Text-to-speech API for Indian languages with streaming support",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if os.getenv("EXPOSE_API_DOCS") == "true" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if os.getenv("EXPOSE_API_DOCS") == "true" else None,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
    value.strip() for value in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if value.strip()
])
cors_origins = [value.strip() for value in os.getenv("CORS_ORIGINS", "").split(",") if value.strip()]
if cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins,
                       allow_methods=["GET", "POST", "PATCH", "PUT"],
                       allow_headers=["Authorization", "Content-Type"])

app.add_exception_handler(RequestValidationError, admin_validation_error)

@app.get("/", include_in_schema=False)
async def playground():
    """Serve the standalone browser test page from the same origin as the API."""
    return FileResponse(
        Path(__file__).parent.parent / "playground.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/integration", include_in_schema=False)
async def public_integration_guide():
    return FileResponse(Path(__file__).parent.parent / "developer-guide.html",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/integration.md", include_in_schema=False)
async def public_integration_markdown():
    return FileResponse(Path(__file__).parent.parent / "PUBLIC_API.md",
                        media_type="text/markdown; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/admin/dashboard.js", include_in_schema=False)
async def admin_script():
    return FileResponse(Path(__file__).parent.parent / "dashboard.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/admin", include_in_schema=False)
async def admin_dashboard():
    return FileResponse(
        Path(__file__).parent.parent / "dashboard.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


# ============================================================================
# Helpers
# ============================================================================

async def audio_stream_converter(
    pcm_stream: AsyncGenerator[bytes, None],
    format: str,
    sample_rate: int = 24000,
    channels: int = 1
) -> AsyncGenerator[bytes, None]:
    """
    Convert PCM stream to target format using ffmpeg.
    
    Args:
        pcm_stream: Async iterator yielding PCM bytes
        format: Target format ('mp3', 'opus', 'aac', 'wav', 'pcm')
        sample_rate: Input sample rate
        channels: Input channels
        
    Yields:
        Encoded audio bytes
    """
    if format == "pcm":
        async for chunk in pcm_stream:
            yield chunk
        return

    # Setup ffmpeg command
    cmd = [
        "ffmpeg",
        "-f", "s16le",       # Input format: signed 16-bit little-endian
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-i", "pipe:0",      # Read from stdin
        "-loglevel", "error" # Suppress output
    ]

    # Format specific flags
    if format == "mp3":
        cmd.extend(["-f", "mp3", "pipe:1"])
    elif format == "opus":
        cmd.extend(["-f", "opus", "pipe:1"])
    elif format == "aac":
        cmd.extend(["-f", "adts", "pipe:1"]) # ADTS is streamable AAC container
    elif format == "wav":
        cmd.extend(["-f", "wav", "pipe:1"])
    else:
        # Fallback to PCM if unknown format
        logger.warning(f"Unknown format '{format}', falling back to PCM")
        async for chunk in pcm_stream:
            yield chunk
        return

    # Start ffmpeg process
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    async def write_stdin():
        try:
            async for chunk in pcm_stream:
                if process.stdin:
                    process.stdin.write(chunk)
                    await process.stdin.drain()
            if process.stdin:
                process.stdin.close()
        except Exception as e:
            logger.error(f"Error writing to ffmpeg stdin: {e}")
            try:
                process.kill()
            except:
                pass

    # Create task to write to stdin
    write_task = asyncio.create_task(write_stdin())

    # Read from stdout
    try:
        if process.stdout:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
    except Exception as e:
        logger.error(f"Error reading from ffmpeg stdout: {e}")
        raise
    finally:
        # Ensure process is cleaned up
        if not write_task.done():
            write_task.cancel()
            try:
                await write_task
            except asyncio.CancelledError:
                pass
        
        if process.returncode is None:
            try:
                process.kill()
            except:
                pass
            await process.wait()


# ============================================================================
# Endpoints
# ============================================================================

def bearer_token(value: str | None) -> str:
    if not value or not value.startswith("Bearer "):
        return ""
    return value[7:].strip()


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # Non-browser clients authenticate with their API key.
    parsed = urlsplit(origin)
    host = websocket.headers.get("host", "").lower()
    if parsed.scheme in ("http", "https") and parsed.netloc.lower() == host:
        return True
    return origin in cors_origins


def access_store(request: Request) -> AccessStore:
    return request.app.state.access


def request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    from api.access import ip_allowed, normalize_networks
    trusted = normalize_networks(os.getenv("TRUSTED_PROXY_IPS", "").split(","))
    return bool(trusted and ip_allowed(peer, trusted) and request.headers.get("x-forwarded-proto") == "https")


def require_admin(request: Request) -> AccessStore:
    address = client_ip(request)
    auth: AdminAuth = request.app.state.admin_auth
    try:
        try:
            local = ipaddress.ip_address(address).is_loopback
        except ValueError:
            local = False
        if not local and not request_is_https(request):
            raise AccessDenied("HTTPS is required for remote administration", 403)
        csrf = auth.session(request.cookies.get(COOKIE_NAME), address)
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), csrf):
            raise AccessDenied("Invalid session token", 403)
    except AccessDenied as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    return access_store(request)


def require_client(request: Request) -> ClientKey:
    store = access_store(request)
    started = time.perf_counter()
    address = client_ip(request)
    try:
        return store.authenticate(bearer_token(request.headers.get("authorization")), address)
    except AccessDenied as exc:
        store.log_request(key=None, address=address, transport="http", route=request.url.path,
                          voice=None, status="denied", duration_ms=int((time.perf_counter() - started) * 1000),
                          error_code=str(exc.status_code))
        raise HTTPException(exc.status_code, exc.message) from None


@app.post("/admin/api/login", include_in_schema=False)
async def admin_login(request: Request, data: AdminLoginRequest):
    address = client_ip(request)
    try:
        try:
            local = ipaddress.ip_address(address).is_loopback
        except ValueError:
            local = False
        if not local and not request_is_https(request):
            raise AccessDenied("HTTPS is required for remote administration", 403)
        token, csrf = request.app.state.admin_auth.login(data.username, data.password, address)
    except AccessDenied as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    response = JSONResponse({"logged_in": True, "csrf_token": csrf}, headers={"Cache-Control": "no-store"})
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS * 3600,
                        httponly=True, secure=request_is_https(request), samesite="strict", path="/admin")
    return response


@app.get("/admin/api/session", include_in_schema=False)
async def admin_session(request: Request, _: AccessStore = Depends(require_admin)):
    csrf = request.app.state.admin_auth.session(request.cookies.get(COOKIE_NAME), client_ip(request))
    return JSONResponse({"logged_in": True, "csrf_token": csrf}, headers={"Cache-Control": "no-store"})


@app.post("/admin/api/logout", include_in_schema=False)
async def admin_logout(request: Request, _: AccessStore = Depends(require_admin)):
    request.app.state.admin_auth.logout(request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"logged_out": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE_NAME, path="/admin", samesite="strict")
    return response


@app.get("/admin/api/overview", include_in_schema=False)
async def admin_overview(
    request: Request,
    hours: int = Query(24, ge=1, le=720),
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    status: Literal["ok", "denied", "invalid", "error", "disconnected"] | None = None,
    transport: Literal["http", "websocket"] | None = None,
    route: Literal["/v1/audio/speech", "/v1/voices"] | None = None,
    key_id: str | None = Query(None, max_length=80),
    ip: str | None = Query(None, max_length=45),
    voice: str | None = Query(None, max_length=80),
    offset: int = Query(0, ge=0, le=100000),
    store: AccessStore = Depends(require_admin),
):
    if ip:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            raise HTTPException(422, "Enter one valid IP address") from None
    try:
        report = store.report(hours, status=status, transport=transport, route=route,
                              key_id=key_id, ip=ip, voice=voice, offset=offset,
                              start_at=start_at, end_at=end_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    report["active_speech"] = request.app.state.limiter.active_total
    report["concurrency_limit"] = request.app.state.limiter.global_concurrent
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@app.get("/admin/api/keys", include_in_schema=False)
async def admin_keys(store: AccessStore = Depends(require_admin)):
    return JSONResponse({"keys": store.list_keys()}, headers={"Cache-Control": "no-store"})


@app.post("/admin/api/keys", include_in_schema=False)
async def admin_create_key(data: KeyCreate, store: AccessStore = Depends(require_admin)):
    try:
        issued = store.create_key(data.label, data.allowed_ips, data.expires_days,
                                  data.max_concurrent, data.requests_per_minute)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return JSONResponse(issued, headers={"Cache-Control": "no-store"})


@app.patch("/admin/api/keys/{key_id}", include_in_schema=False)
async def admin_update_key(key_id: str, data: KeyUpdate, store: AccessStore = Depends(require_admin)):
    try:
        found = store.update_key(key_id, data.allowed_ips, data.max_concurrent, data.requests_per_minute)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not found:
        raise HTTPException(404, "Key not found")
    return {"updated": True}


@app.post("/admin/api/keys/{key_id}/revoke", include_in_schema=False)
async def admin_revoke_key(key_id: str, store: AccessStore = Depends(require_admin)):
    if not store.revoke_key(key_id):
        raise HTTPException(404, "Key not found")
    return {"revoked": True}


@app.delete("/admin/api/keys/{key_id}", include_in_schema=False)
async def admin_delete_key(key_id: str, store: AccessStore = Depends(require_admin)):
    if not store.delete_revoked_key(key_id):
        raise HTTPException(409, "Only a revoked key can be deleted")
    return JSONResponse({"deleted": True}, headers={"Cache-Control": "no-store"})


@app.get("/admin/api/allowlist", include_in_schema=False)
async def admin_allowlist(store: AccessStore = Depends(require_admin)):
    return {"allowed_ips": store.allowed_ips()}


@app.put("/admin/api/allowlist", include_in_schema=False)
async def admin_set_allowlist(data: AllowlistUpdate, store: AccessStore = Depends(require_admin)):
    try:
        return {"allowed_ips": store.set_allowed_ips(data.allowed_ips)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration."""
    return {"status": "healthy"}


@app.get("/v1/voices", response_model=VoicesResponse)
async def get_voices(request: Request, key: ClientKey = Depends(require_client)):
    """
    Get list of available voices.
    
    Returns:
        List of available voices with metadata
    """
    voices = get_all_voices(model_id="svara-tts-v1")
    request.app.state.access.log_request(key=key, address=client_ip(request), transport="http",
                                          route="/v1/voices", voice=None, status="ok", duration_ms=0)
    return VoicesResponse(
        voices=[VoiceResponse(**voice.to_dict()) for voice in voices]
    )

@app.websocket("/v1/audio/speech/ws")
async def websocket_speech(websocket: WebSocket):
    """One request per connection: JSON request, JSON start, binary PCM, JSON done.

    PCM is signed 16-bit little-endian, mono, 24 kHz. Closing the socket
    cancels generation for this client. HTTP streaming remains available at
    POST /v1/audio/speech with stream=true and response_format=pcm.
    """
    app_state = websocket.scope["app"].state
    store: AccessStore = app_state.access
    limiter: RequestLimiter = app_state.limiter
    address = client_ip(websocket)
    started = time.perf_counter()
    key: ClientKey | None = None
    acquired = False
    status = "error"
    error_code = None
    voice_id = None
    first_audio_ms = None
    audio_bytes = 0
    if not websocket_origin_allowed(websocket):
        store.log_request(key=None, address=address, transport="websocket",
                          route="/v1/audio/speech", voice=None, status="denied",
                          duration_ms=0, error_code="origin")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        payload = json.loads(await websocket.receive_text())
        if not isinstance(payload, dict):
            raise ValueError("Request must be a JSON object")
        raw_key = payload.pop("api_key", "")
        key = store.authenticate(raw_key, address)
        req = OpenAISpeechRequest.model_validate(payload)
        voice_id = req.voice
        voice = get_voice(req.voice)
        if voice is None or voice.model_id != "svara-tts-v1":
            raise ValueError("Choose a valid preset voice")
        if req.reference_audio:
            raise ValueError("WebSocket voice cloning is not supported; use the HTTP endpoint")
        await limiter.acquire(key)
        acquired = True

        gen_kwargs = {name: value for name, value in {
            "temperature": req.temperature, "top_p": req.top_p,
            "top_k": req.top_k, "repetition_penalty": req.repetition_penalty,
            "max_tokens": req.max_tokens,
        }.items() if value is not None}
        await websocket.send_json({
            "type": "start", "format": "pcm_s16le", "sample_rate": 24000,
            "channels": 1, "voice": req.voice,
        })
        pcm_source = orchestrator.astream(
            text=req.input, speaker_id=get_speaker_id(req.voice),
            chunk_size=req.chunk_size, buffer_ms=req.buffer_ms, **gen_kwargs,
        )
        if req.steady_volume:
            pcm_source = normalize_pcm_stream(pcm_source)
        async for chunk in pcm_source:
            if chunk:
                if first_audio_ms is None:
                    first_audio_ms = int((time.perf_counter() - started) * 1000)
                audio_bytes += len(chunk)
                await websocket.send_bytes(chunk)
        status = "ok"
        await websocket.send_json({"type": "done"})
        await websocket.close()
    except AccessDenied as exc:
        status, error_code = "denied", str(exc.status_code)
        await websocket.send_json({"type": "error", "message": exc.message})
        await websocket.close(code=1008)
    except (ValidationError, ValueError):
        status, error_code = "invalid", "400"
        await websocket.send_json({"type": "error", "message": "Invalid speech request"})
        await websocket.close(code=1008)
    except WebSocketDisconnect:
        status = "disconnected"
    except asyncio.CancelledError:
        status = "disconnected"
        raise
    except Exception:
        status, error_code = "error", "500"
        logger.exception("WebSocket speech generation failed")
        try:
            await websocket.send_json({"type": "error", "message": "Speech generation failed"})
            await websocket.close(code=1011)
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        if acquired and key:
            await limiter.release(key)
        store.log_request(key=key, address=address, transport="websocket",
                          route="/v1/audio/speech", voice=voice_id, status=status,
                          duration_ms=int((time.perf_counter() - started) * 1000),
                          first_audio_ms=first_audio_ms, audio_bytes=audio_bytes,
                          error_code=error_code, audio_seconds=audio_bytes / 48000)


async def build_audio_stream(req: OpenAISpeechRequest, usage_counter: dict | None = None):
    """
    OpenAI-compatible text-to-speech endpoint.

    Drop-in replacement for OpenAI's /v1/audio/speech. Works with the OpenAI SDK
    out of the box. Extended features (zero-shot cloning, generation params) can
    be passed via the SDK's `extra_body` parameter.

    Supports:
    - Standard TTS with any available voice
    - Zero-shot voice cloning via `reference_audio` (base64)
    - Generation parameter tuning (temperature, top_p, etc.)
    - Streaming and non-streaming responses
    - Multiple audio formats (mp3, opus, aac, wav, pcm)
    """
    # Resolve voice: try voice_id lookup first, then treat as direct speaker name
    speaker_id = None
    if not req.reference_audio:
        try:
            speaker_id = get_speaker_id(req.voice)
        except ValueError:
            # Maybe it's already a speaker name like "Hindi (Male)"
            # Pass it through and let the orchestrator handle it
            speaker_id = req.voice

    # Zero-shot voice cloning
    audio_tokens = None
    if req.reference_audio:
        logger.info(f"Loading reference audio from bytes ({len(req.reference_audio)} bytes)")
        audio_tensor, sample_rate = load_audio_from_bytes(req.reference_audio, device=SNAC_DEVICE)
        logger.info(f"Audio loaded: shape={audio_tensor.shape}, sr={sample_rate}Hz")

        codec = SNACCodec(device=SNAC_DEVICE)
        audio_tokens = codec.encode_audio(audio_tensor, input_sample_rate=sample_rate, add_token_offsets=True)
        logger.info(f"Audio tokens encoded: {len(audio_tokens)} tokens")

    # Build generation kwargs
    gen_kwargs = {}
    if req.temperature is not None:
        gen_kwargs["temperature"] = req.temperature
    if req.top_p is not None:
        gen_kwargs["top_p"] = req.top_p
    if req.top_k is not None:
        gen_kwargs["top_k"] = req.top_k
    if req.repetition_penalty is not None:
        gen_kwargs["repetition_penalty"] = req.repetition_penalty
    if req.max_tokens is not None:
        gen_kwargs["max_tokens"] = req.max_tokens

    # Map format to media type
    format_media_types = {
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "aac": "audio/aac",
        "wav": "audio/wav",
        "pcm": "audio/pcm",
    }
    media_type = format_media_types.get(req.response_format, "audio/mpeg")

    # Generate audio
    pcm_generator = orchestrator.astream(
        text=req.input,
        audio_reference=audio_tokens,
        reference_text=req.reference_transcript,
        speaker_id=speaker_id,
        chunk_size=req.chunk_size,
        buffer_ms=req.buffer_ms,
        **gen_kwargs,
    )
    if req.steady_volume:
        pcm_generator = normalize_pcm_stream(pcm_generator)

    if usage_counter is not None:
        source = pcm_generator
        async def metered_pcm():
            async for pcm in source:
                usage_counter['seconds'] += len(pcm) / 48000  # mono PCM16 at 24 kHz
                yield pcm
        pcm_generator = metered_pcm()

    audio_stream = audio_stream_converter(
        pcm_generator,
        format=req.response_format,
    )

    return audio_stream, media_type


@app.post("/v1/audio/speech")
async def openai_speech(request: Request, req: OpenAISpeechRequest,
                        key: ClientKey = Depends(require_client)):
    """Authenticate, limit, meter and stream a speech request."""
    store: AccessStore = request.app.state.access
    limiter: RequestLimiter = request.app.state.limiter
    address = client_ip(request)
    started = time.perf_counter()
    try:
        await limiter.acquire(key)
    except AccessDenied as exc:
        store.log_request(key=key, address=address, transport="http", route="/v1/audio/speech",
                          voice=req.voice, status="denied", duration_ms=0,
                          error_code=str(exc.status_code))
        raise HTTPException(exc.status_code, exc.message) from None

    try:
        usage_counter = {'seconds': 0.0}
        audio_stream, media_type = await build_audio_stream(req, usage_counter)
    except Exception:
        await limiter.release(key)
        store.log_request(key=key, address=address, transport="http", route="/v1/audio/speech",
                          voice=req.voice, status="error", duration_ms=int((time.perf_counter()-started)*1000),
                          error_code="500")
        raise

    async def metered_stream():
        status = "error"
        first_audio_ms = None
        audio_bytes = 0
        try:
            async for chunk in audio_stream:
                if chunk:
                    if first_audio_ms is None:
                        first_audio_ms = int((time.perf_counter() - started) * 1000)
                    audio_bytes += len(chunk)
                    yield chunk
            status = "ok"
        except asyncio.CancelledError:
            status = "disconnected"
            raise
        except Exception:
            logger.exception("HTTP speech generation failed")
            raise
        finally:
            await limiter.release(key)
            store.log_request(key=key, address=address, transport="http", route="/v1/audio/speech",
                              voice=req.voice, status=status, duration_ms=int((time.perf_counter()-started)*1000),
                              first_audio_ms=first_audio_ms, audio_bytes=audio_bytes,
                              error_code="500" if status == "error" else None,
                              audio_seconds=usage_counter["seconds"])

    if req.stream:
        return StreamingResponse(metered_stream(), media_type=media_type, headers={
            "X-Sample-Rate": "24000", "X-Channels": "1", "Cache-Control": "no-store",
        })
    audio_chunks = [chunk async for chunk in metered_stream()]
    return Response(content=b"".join(audio_chunks), media_type=media_type,
                    headers={"Cache-Control": "no-store"})


# ============================================================================
# Main Entry Point (for local development only)
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("API_PORT", "8080"))
    host = os.getenv("API_HOST", "0.0.0.0")
    
    print(f"Starting speech API on {host}:{port}")
    print("Note: For production, use supervisord to manage processes")
    
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
