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


# Echo requests per check. The check succeeds if ANY reply arrives, so a
# single dropped packet (routine on a LAN, especially across subnets) can
# no longer produce a false "down" result. A real DOWN now requires
# PING_COUNT * failure_threshold consecutive lost packets.
PING_COUNT = 3


def check_icmp(hostname: str, timeout_seconds: int) -> CheckResult:
    try:
        host = ping(
            hostname,
            count=PING_COUNT,
            # Per-packet timeout. Spread the budget so a dead host costs
            # roughly timeout_seconds total, not PING_COUNT * timeout_seconds.
            timeout=max(1.0, timeout_seconds / PING_COUNT),
            privileged=False,
            interval=0.2,
        )
    except ICMPLibError as exc:
        return CheckResult(success=False, error=f"icmp error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(success=False, error=f"icmp error: {exc}")

    if host.packets_received > 0:
        return CheckResult(success=True, latency_ms=int(round(host.avg_rtt)))

    # Helpful hint: ICMP from a Docker Desktop (Mac/Win) container cannot
    # reach LAN hosts because the LinuxKit VM bridge does not forward ICMP
    # echo-replies from the host's local network. Use a TCP port check instead.
    if _is_private_target(hostname):
        return CheckResult(
            success=False,
            error=f"no reply to {PING_COUNT} pings (LAN target — if running on Docker Desktop Mac/Win, ICMP cannot reach 192.168.x.x; use a TCP port check)",
        )
    return CheckResult(success=False, error=f"no reply to {PING_COUNT} pings")
