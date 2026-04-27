import ipaddress
import socket

from icmplib import ICMPLibError, ping

from . import CheckResult


def _is_private_target(hostname: str) -> bool:
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            addr = ipaddress.ip_address(socket.gethostbyname(hostname))
        except (socket.gaierror, ValueError):
            return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def check_icmp(hostname: str, timeout_seconds: int) -> CheckResult:
    try:
        host = ping(
            hostname,
            count=1,
            timeout=timeout_seconds,
            privileged=False,
            interval=0.2,
        )
    except ICMPLibError as exc:
        return CheckResult(success=False, error=f"icmp error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(success=False, error=f"icmp error: {exc}")

    if host.is_alive and host.packets_received > 0:
        return CheckResult(success=True, latency_ms=int(round(host.avg_rtt)))

    # Helpful hint: ICMP from a Docker Desktop (Mac/Win) container cannot
    # reach LAN hosts because the LinuxKit VM bridge does not forward ICMP
    # echo-replies from the host's local network. Use a TCP port check instead.
    if _is_private_target(hostname):
        return CheckResult(
            success=False,
            error="no reply (LAN target — if running on Docker Desktop Mac/Win, ICMP cannot reach 192.168.x.x; use a TCP port check)",
        )
    return CheckResult(success=False, error="no reply")
