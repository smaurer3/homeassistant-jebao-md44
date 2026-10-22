"""GizWits LAN discovery for the MD-4.4 (UDP broadcast on port 12414).

Sending the 8-byte ``0x0003`` discovery frame to a subnet broadcast address
makes every GizWits device on the LAN reply with a ``0x0004`` frame carrying
length-prefixed fields: did, mac, wifi firmware version, product key, then
the cloud host / SDK / MCU version. That gives us everything needed to
identify the pump without touching the cloud.

Note: broadcasting to 255.255.255.255 is unreliable on multi-homed hosts
(the datagram leaves via whichever interface the routing table picks). We
broadcast per-interface instead.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from typing import List

from .const import SUPPORTED_PRODUCT_KEYS, UDP_DISCOVERY_PORT

_LOGGER = logging.getLogger(__name__)

DISCOVERY_FRAME = bytes.fromhex("0000000303000003")


@dataclass
class DiscoveredPump:
    host: str
    did: str
    mac: str
    firmware: str
    product_key: str

    @property
    def supported(self) -> bool:
        return self.product_key in SUPPORTED_PRODUCT_KEYS

    @property
    def mac_pretty(self) -> str:
        m = self.mac
        return ":".join(m[i:i + 2] for i in range(0, len(m), 2)) if len(m) == 12 else m


def _parse(data: bytes, host: str) -> DiscoveredPump | None:
    """Parse a 0x0004 discovery reply."""
    if len(data) < 12 or data[:4] != b"\x00\x00\x00\x03":
        return None
    i = 4
    while i < len(data) and data[i] & 0x80:   # skip varint length
        i += 1
    i += 1
    if i + 3 > len(data) or data[i + 1] != 0x00 or data[i + 2] != 0x04:
        return None
    i += 3

    fields: List[bytes] = []
    while i + 2 <= len(data) and len(fields) < 4:
        ln = int.from_bytes(data[i:i + 2], "big")
        i += 2
        if ln == 0 or i + ln > len(data):
            break
        fields.append(data[i:i + ln])
        i += ln
    if len(fields) < 4:
        return None

    def txt(b: bytes) -> str:
        try:
            return b.decode("ascii").strip("\x00")
        except UnicodeDecodeError:
            return b.hex()

    return DiscoveredPump(
        host=host,
        did=txt(fields[0]),
        mac=fields[1].hex(),
        firmware=txt(fields[2]),
        product_key=txt(fields[3]),
    )


def _broadcast_addresses() -> List[str]:
    """Best-effort list of subnet broadcast addresses for local IPv4 NICs."""
    addrs: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            addrs.append(".".join(ip.split(".")[:3]) + ".255")
    except OSError as err:
        _LOGGER.debug("Could not enumerate interfaces: %s", err)
    addrs.append("255.255.255.255")
    seen: List[str] = []
    for a in addrs:
        if a not in seen:
            seen.append(a)
    return seen


def _discover_blocking(timeout: float) -> List[DiscoveredPump]:
    found: dict[str, DiscoveredPump] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.4)
    try:
        for bcast in _broadcast_addresses():
            try:
                sock.sendto(DISCOVERY_FRAME, (bcast, UDP_DISCOVERY_PORT))
            except OSError as err:
                _LOGGER.debug("Broadcast to %s failed: %s", bcast, err)
        loop_end = asyncio.get_event_loop().time() if False else None  # noqa
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            pump = _parse(data, addr[0])
            if pump is not None:
                found[pump.did] = pump
    finally:
        sock.close()
    return list(found.values())


async def async_discover(timeout: float = 3.0) -> List[DiscoveredPump]:
    """Broadcast for pumps. Never raises — returns [] on failure."""
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, _discover_blocking, timeout
        )
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Discovery failed: %s", err)
        return []


async def async_find_by_mac(mac: str, timeout: float = 3.0) -> DiscoveredPump | None:
    """Locate a known pump by MAC — used to recover a changed DHCP address."""
    want = mac.lower().replace(":", "").replace("-", "")
    for pump in await async_discover(timeout):
        if pump.mac.lower() == want:
            return pump
    return None
