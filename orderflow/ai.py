"""AI boundary. No external request is made by this PoC."""

from __future__ import annotations

import time
from typing import Protocol


class AIError(Exception):
    def __init__(self, code: str, *, upstream_http_status: int | None = None,
                 upstream_reason: str | None = None):
        self.code = code
        self.upstream_http_status = upstream_http_status
        self.upstream_reason = upstream_reason
        super().__init__(code)


class AIUnknown(AIError):
    """The call may have been processed; do not replay it automatically."""


class AIAdapter(Protocol):
    def recognize(self, pdf_path: str, *, deadline_seconds: float) -> object:
        """Return parsed data; keep credentials, timeouts and raw responses inside the adapter."""


class MockAdapter:
    def __init__(self, scenario: str):
        self.scenario = scenario

    def recognize(self, pdf_path: str, *, deadline_seconds: float) -> object:
        # The mock never reads the document. Its result is fixed test data.
        time.sleep(min(0.15, deadline_seconds))
        if self.scenario == "fail":
            raise AIError("AI_UNAVAILABLE")
        if self.scenario == "timeout":
            raise AIUnknown("AI_TIMEOUT_UNKNOWN")
        if self.scenario == "invalid":
            return {"items": "wrong shape"}
        return {"items": [{"description": "模擬品項 <測試> & \"引號\"", "quantity": 2},
                          {"description": "Second / item", "quantity": 1}]}


class ExternalAdapter:
    """Integration seam; provider contract and permission must be decided first."""

    def recognize(self, pdf_path: str, *, deadline_seconds: float) -> object:
        raise AIError("AI_NOT_CONFIGURED")
