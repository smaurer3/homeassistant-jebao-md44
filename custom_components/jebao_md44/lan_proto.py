"""GizWits LAN protocol (TCP/12416) framing + P0 codec for the MD-4.4.

The pump speaks the GizWits LAN protocol on TCP port 12416. Reads always
worked; writes were long assumed to be firmware-disabled, but that was
wrong — the frames being sent were simply malformed. Two details matter,
both verified against a live pump:

1. ``attr_flags`` is sized from the number of *writable* datapoints —
   39 for this product, so **5 bytes**. Earlier attempts used 2-4 bytes
   (copied from the older 4-channel doser, which has 22 writable
   datapoints and therefore 3 flag bytes) and the MCU silently dropped
   them while the WiFi module still ACKed.
2. ``attr_flags`` and the packed bool/enum block are **big-endian
   fields**: the bit index counts from the LSB of the whole field, not
   of byte 0. So ``switch`` lives in byte 2 bit 0, and the flag bit for
   datapoint ``i`` is ``flags[4 - i // 8] |= 1 << (i % 8)``.

Layout below is the authoritative GizWits datapoint schema for product
``1aa33c38ba9d4b78a9e7796705b2fad7`` (4-head doser, WiFi+BLE, ESP32-C3).
It is bundled rather than fetched so local mode needs no cloud access at
all. A decoded read was cross-checked field-by-field against the cloud's
own view and matched on all 42 datapoints, including the eight 96-byte
schedule blobs.
"""
from __future__ import annotations

from typing import Any, Dict, List

MAGIC = b"\x00\x00\x00\x03"

# Message types.
MSG_PASSCODE_REQ = 0x06
MSG_PASSCODE_RES = 0x07
MSG_LOGIN_REQ = 0x08
MSG_LOGIN_RES = 0x09
MSG_STATUS_REQ = 0x90
MSG_STATUS_RES = 0x91     # also arrives unsolicited when the pump changes
MSG_CONTROL_REQ = 0x93
MSG_CONTROL_RES = 0x94

ACTION_WRITE = 0x01
ACTION_READ = 0x02
ACTION_REPORT = 0x03

# --- P0 layout -------------------------------------------------------------
# Bools/enums are packed into a 3-byte big-endian field at bytes 0..2, with
# bit offsets counted from that field's LSB.
BOOL_FIELD_BYTES = 3
P0_LEN = 792          # full P0 buffer the pump reports
WRITABLE_LEN = 789    # writable area: bytes 0..788 (ends after HMSData)
FLAG_BYTES = 5        # ceil(39 writable datapoints / 8)

# name -> (datapoint id, kind, byte_offset, bit_offset, length)
#   kind: "bit" (bit_offset is a global offset into the bool field),
#         "u8", or "bin".
# The datapoint id doubles as the attr_flags bit index: writable ids are
# 0..38 contiguous for this product.
LAYOUT: Dict[str, tuple] = {
    "switch": (0, "bit", 0, 0, 1),
    **{f"channe{i}": (i, "bit", 0, i, 1) for i in range(1, 9)},
    **{f"Timer{i}ON": (8 + i, "bit", 0, 8 + i, 1) for i in range(1, 9)},
    "CALSW": (17, "bit", 0, 17, 1),
    "CALSet": (18, "bit", 0, 18, 3),
    "channelTTL": (19, "u8", 3, 0, 1),
    **{f"IntervalT{i}": (19 + i, "u8", 3 + i, 0, 1) for i in range(1, 9)},
    "Calib1": (28, "u8", 12, 0, 1),
    **{f"CH{i}SWTime": (28 + i, "bin", 13 + 96 * (i - 1), 0, 96) for i in range(1, 9)},
    "YMDData": (37, "bin", 781, 0, 4),
    "HMSData": (38, "bin", 785, 0, 4),
    # Read-only tail. These are NOT in the packed bool field — each has its
    # own byte, which is why decoding them from the bit field returns the
    # value of ``switch`` instead.
    "time1": (39, "u8", 789, 0, 1),
    "OpenCircuit": (40, "own_bit", 790, 0, 1),
    "Fault_UART": (41, "own_bit", 791, 0, 1),
}

SCHEDULES_PER_CHANNEL = 24
SCHEDULE_ENTRY_LEN = 4
SCHEDULE_BLOB_LEN = 96


class LanProtocolError(Exception):
    """Malformed frame or unexpected response."""


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def build_frame(body: bytes) -> bytes:
    return MAGIC + encode_varint(len(body)) + body


def build_passcode_request() -> bytes:
    return build_frame(bytes([0x00, 0x00, MSG_PASSCODE_REQ]))


def build_login(passcode: bytes) -> bytes:
    """Echo the passcode the pump just handed us. The pump supplies it over
    the LAN itself, so local control needs no cloud credentials."""
    return build_frame(
        bytes([0x00, 0x00, MSG_LOGIN_REQ])
        + len(passcode).to_bytes(2, "big")
        + passcode
    )


def build_status_request() -> bytes:
    return build_frame(bytes([0x00, 0x00, MSG_STATUS_REQ, ACTION_READ]))


def _flag_bits(dp_ids: List[int]) -> bytes:
    """attr_flags as a big-endian field, bit index from its LSB."""
    flags = bytearray(FLAG_BYTES)
    for dp_id in dp_ids:
        flags[FLAG_BYTES - 1 - (dp_id // 8)] |= 1 << (dp_id % 8)
    return bytes(flags)


def set_bits(buf: bytearray, bit_offset: int, nbits: int, value: int) -> None:
    field = int.from_bytes(buf[:BOOL_FIELD_BYTES], "big")
    mask = ((1 << nbits) - 1) << bit_offset
    field = (field & ~mask) | ((value << bit_offset) & mask)
    buf[:BOOL_FIELD_BYTES] = field.to_bytes(BOOL_FIELD_BYTES, "big")


def get_bits(p0: bytes, bit_offset: int, nbits: int) -> int:
    field = int.from_bytes(p0[:BOOL_FIELD_BYTES], "big")
    return (field >> bit_offset) & ((1 << nbits) - 1)


def build_control(current_p0: bytes, changes: Dict[str, Any], sn: int) -> bytes:
    """Build a 0x93 write frame.

    ``attr_vals`` is a byte-for-byte clone of the pump's current writable
    area with only the requested datapoints changed. Cloning (rather than
    sending a sparse payload) is what the firmware expects, and it has a
    useful safety property: a flag bit landing on the wrong datapoint just
    rewrites that datapoint with its own unchanged value.
    """
    if len(current_p0) < WRITABLE_LEN:
        raise LanProtocolError(
            f"P0 too short to build a write: {len(current_p0)} < {WRITABLE_LEN}"
        )
    vals = bytearray(current_p0[:WRITABLE_LEN])
    dp_ids: List[int] = []

    for name, value in changes.items():
        try:
            dp_id, kind, byte_off, bit_off, length = LAYOUT[name]
        except KeyError as err:
            raise LanProtocolError(f"unknown datapoint {name!r}") from err
        if kind == "bit":
            set_bits(vals, bit_off, length, int(value))
        elif kind == "u8":
            vals[byte_off] = max(0, min(255, int(value)))
        elif kind == "bin":
            raw = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
            if len(raw) != length:
                raise LanProtocolError(
                    f"{name}: expected {length} bytes, got {len(raw)}"
                )
            vals[byte_off:byte_off + length] = raw
        else:
            raise LanProtocolError(f"{name} is read-only")
        dp_ids.append(dp_id)

    body = (
        bytes([0x00, 0x00, MSG_CONTROL_REQ])
        + sn.to_bytes(4, "big")
        + bytes([ACTION_WRITE])
        + _flag_bits(dp_ids)
        + bytes(vals)
    )
    return build_frame(body)


def p0_from_status_body(body: bytes) -> bytes:
    """Extract the P0 buffer from a 0x91 report body (``00 00 91 03`` + P0)."""
    if len(body) < 8 or body[2] != MSG_STATUS_RES:
        raise LanProtocolError("not a 0x91 status body")
    return body[4:]


def decode_p0(p0: bytes) -> Dict[str, Any]:
    """Decode a P0 buffer into raw datapoint values keyed by GizWits name."""
    if len(p0) < WRITABLE_LEN:
        raise LanProtocolError(f"P0 too short: {len(p0)}")
    out: Dict[str, Any] = {}
    for name, (_dp_id, kind, byte_off, bit_off, length) in LAYOUT.items():
        if kind == "bit":
            out[name] = get_bits(p0, bit_off, length)
        elif kind == "own_bit":
            out[name] = (p0[byte_off] >> bit_off) & 1 if byte_off < len(p0) else 0
        elif kind == "u8":
            out[name] = p0[byte_off] if byte_off < len(p0) else 0
        else:
            out[name] = p0[byte_off:byte_off + length].hex()
    return out
