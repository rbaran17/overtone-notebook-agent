"""Caller-facing errors."""

from __future__ import annotations


class OvertoneError(Exception):
    """Raised for failures the caller should see. `status` maps to an HTTP status."""

    def __init__(self, message: str, *, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:
        return self.message
