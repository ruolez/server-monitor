from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    success: bool
    latency_ms: int | None = None
    error: str | None = None
