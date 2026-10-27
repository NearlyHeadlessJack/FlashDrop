"""Application-level errors safe to show in the UI."""

from __future__ import annotations


class UserError(Exception):
    def __init__(self, message: str, *, code: str = "bad_request") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
