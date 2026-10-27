"""Dashboard input limits shared by the API and browser."""
from __future__ import annotations
from typing import Annotated

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from api.access import normalize_networks

IPAddress = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=64)]

class AdminInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

class KeyCreate(AdminInput):
    label: str = Field(strict=True, min_length=1, max_length=80)
    allowed_ips: list[IPAddress] = Field(default_factory=list, max_length=100)
    expires_days: int = Field(default=90, strict=True, ge=1, le=3650)
    max_concurrent: int = Field(default=2, strict=True, ge=1, le=16)
    requests_per_minute: int = Field(default=60, strict=True, ge=1, le=600)
    priority: int = Field(default=2, strict=True, ge=1, le=3)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a client name")
        return value

    @field_validator("allowed_ips")
    @classmethod
    def clean_ips(cls, values: list[str]) -> list[str]:
        return normalize_networks(values)

class KeyUpdate(AdminInput):
    allowed_ips: list[IPAddress] = Field(max_length=100)
    max_concurrent: int = Field(strict=True, ge=1, le=16)
    requests_per_minute: int = Field(strict=True, ge=1, le=600)
    priority: int | None = Field(default=None, strict=True, ge=1, le=3)

    @field_validator("allowed_ips")
    @classmethod
    def clean_ips(cls, values: list[str]) -> list[str]:
        return normalize_networks(values)

class AllowlistUpdate(AdminInput):
    allowed_ips: list[IPAddress] = Field(max_length=100)

    @field_validator("allowed_ips")
    @classmethod
    def clean_ips(cls, values: list[str]) -> list[str]:
        return normalize_networks(values)

class AdminLoginRequest(AdminInput):
    username: str = Field(strict=True, min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(strict=True, min_length=1, max_length=1024)

FIELD_MESSAGES = {
    "username": "Use 3–80 letters, numbers, underscores or hyphens.",
    "password": "Enter the dashboard password (up to 1,024 characters).",
    "label": "Enter a client name of 1–80 characters.",
    "allowed_ips": "Use at most 100 valid IP addresses or CIDR ranges, one per line.",
    "expires_days": "Enter a whole number from 1 to 3,650.",
    "max_concurrent": "Enter a whole number from 1 to 16.",
    "requests_per_minute": "Enter a whole number from 1 to 600.",
    "priority": "Choose High, Normal or Low priority.",
    "key_id": "Select a valid client key.",
    "hours": "Choose a period between 1 and 720 hours.",
    "offset": "Choose a page offset between 0 and 100,000.",
    "ip": "Enter one valid IPv4 or IPv6 address.",
    "voice": "Enter a voice ID of up to 80 characters.",
}

async def admin_validation_error(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith("/admin/api/"):
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, exc)
    errors = []
    for item in exc.errors():
        field = next((part for part in reversed(item.get("loc", ())) if isinstance(part, str)), "request")
        errors.append({"field": field, "message": FIELD_MESSAGES.get(field, "Check this value and try again.")})
    return JSONResponse({"detail": "Check the highlighted fields.", "errors": errors}, status_code=422,
                        headers={"Cache-Control": "no-store"})
