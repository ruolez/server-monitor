import socket
import time

from . import CheckResult


def check_tcp(hostname: str, port: int, timeout_seconds: int) -> CheckResult:
    start = time.monotonic()
    try:
        with socket.create_connection((hostname, port), timeout=timeout_seconds):
            latency_ms = int((time.monotonic() - start) * 1000)
            return CheckResult(success=True, latency_ms=latency_ms)
    except socket.timeout:
        return CheckResult(success=False, error=f"tcp timeout after {timeout_seconds}s")
    except OSError as exc:
        return CheckResult(success=False, error=f"tcp error: {exc}")
