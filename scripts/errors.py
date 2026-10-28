from __future__ import annotations

from typing import Any


class AuditError(RuntimeError):
    """An input or global read failure safe to show to the operator."""


class StudioReadError(AuditError):
    def __init__(
        self,
        stage: str,
        summary: str,
        http_status: int | None = None,
        *,
        auth_failure: bool = False,
    ):
        super().__init__(summary)
        self.stage = stage
        self.http_status = http_status
        self.safe_summary = summary
        self.auth_failure = auth_failure


class GlobalAuthenticationError(StudioReadError):
    def __init__(self, failed_stage: str, http_status: int | None):
        super().__init__(
            "authentication",
            "Studio authentication could not be recovered.",
            http_status,
            auth_failure=True,
        )
        self.failed_stage = failed_stage
        self.completed_checks: list[dict[str, Any]] = []


class AuditAuthenticationError(AuditError):
    def __init__(self, summary: str, http_status: int | None = None):
        super().__init__(summary)
        self.http_status = http_status
        self.auth_failure = True
