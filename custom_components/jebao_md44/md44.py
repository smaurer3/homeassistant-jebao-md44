"""Jebao MD-4.4 dosing pump — local LAN device class.

Talks to the pump directly over the GizWits LAN protocol on TCP/12416. No
cloud account, no internet access, no credentials: the pump hands out its
own passcode over the LAN during the login handshake.

Design notes:

* The connection is held open. The pump sends an unsolicited ``0x91``
  status report whenever anything changes (verified: a change made from the
  phone app showed up on the LAN socket ~2.7 s later), so state is pushed
  to Home Assistant in near real time rather than polled.
* A background supervisor reconnects with exponential backoff, and a
  lightweight keepalive status request keeps the socket warm and doubles as
  a liveness check.
* Writes clone the pump's current writable area and change only the
  requested datapoints — see ``lan_proto.build_control`` for why.

Earlier versions of this work concluded the firmware ignored LAN writes.
That was wrong: the frames were malformed (wrong ``attr_flags`` width and
byte order). Writes are confirmed working for bool, uint8 and the 96-byte
binary schedule blobs.
"""
from __future__ import annotations

import asyncio
import binascii
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import lan_proto as proto
from .const import MD44_CHANNEL_COUNT, TCP_PORT

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0
KEEPALIVE_SECONDS = 60.0
ACK_TIMEOUT = 5.0
STATUS_TIMEOUT = 6.0


class MD44Error(Exception):
    """Top-level error for callers."""


class MD44ConnectionError(MD44Error):
    """Could not reach the pump / login failed."""


@dataclass
class ScheduleEntry:
    hour: int
    minute: int
    quantity: int  # whole-mL units as stored by the firmware, max 255

    @property
    def quantity_ml(self) -> float:
        return float(self.quantity)


@dataclass
class MD44State:
    master_on: bool = False
    channels: List[bool] = field(default_factory=lambda: [False] * MD44_CHANNEL_COUNT)
    timers_enabled: List[bool] = field(default_factory=lambda: [False] * MD44_CHANNEL_COUNT)
    intervals_days: List[int] = field(default_factory=lambda: [0] * MD44_CHANNEL_COUNT)
    schedules: List[List[ScheduleEntry]] = field(
        default_factory=lambda: [[] for _ in range(MD44_CHANNEL_COUNT)]
    )
    cal_switch: bool = False
    cal_set: str = ""
    calib1: int = 0
    ymd: str = "00000000"
    hms: str = "00000000"
    channel_ttl: int = 0
    time1: int = 0
    open_circuit: bool = False
    fault_uart: bool = False

    def as_dict(self) -> dict:
        return {
            "master_on": self.master_on,
            "channels": list(self.channels),
            "timers_enabled": list(self.timers_enabled),
            "intervals_days": list(self.intervals_days),
            "cal_switch": self.cal_switch,
            "cal_set": self.cal_set,
            "calib1": self.calib1,
            "ymd": self.ymd,
            "hms": self.hms,
            "channel_ttl": self.channel_ttl,
            "open_circuit": self.open_circuit,
            "fault_uart": self.fault_uart,
            "schedules": [
                [(s.hour, s.minute, s.quantity) for s in chan]
                for chan in self.schedules
            ],
        }


def _parse_schedule_blob(raw: str) -> List[ScheduleEntry]:
    """Parse a 96-byte CH*SWTime blob: 24 x ``[hour, minute, reserved, mL]``.

    Verified by setting a known schedule from the app (CH1 = 3 mL @ 12:00)
    and reading back ``0c 00 00 03`` at bytes 0..3. All-zero entries are
    skipped.
    """
    if not raw:
        return []
    try:
        blob = bytes.fromhex(raw) if isinstance(raw, str) else bytes(raw)
    except (binascii.Error, ValueError, TypeError):
        return []
    out: List[ScheduleEntry] = []
    for i in range(proto.SCHEDULES_PER_CHANNEL):
        base = i * proto.SCHEDULE_ENTRY_LEN
        if base + proto.SCHEDULE_ENTRY_LEN > len(blob):
            break
        hour, minute, _reserved, quantity = blob[base:base + proto.SCHEDULE_ENTRY_LEN]
        if hour == 0 and minute == 0 and quantity == 0:
            continue
        out.append(ScheduleEntry(hour=hour, minute=minute, quantity=quantity))
    return out


def _schedule_to_blob(entries: List[ScheduleEntry]) -> str:
    """Pack entries into the 96-byte hex blob. Inverse of the above."""
    blob = bytearray(proto.SCHEDULE_BLOB_LEN)
    for i, entry in enumerate(entries[:proto.SCHEDULES_PER_CHANNEL]):
        base = i * proto.SCHEDULE_ENTRY_LEN
        blob[base] = int(entry.hour) & 0xFF
        blob[base + 1] = int(entry.minute) & 0xFF
        blob[base + 2] = 0
        blob[base + 3] = max(0, min(255, int(entry.quantity)))
    return bytes(blob).hex()


def _state_from_raw(raw: Dict[str, Any]) -> MD44State:
    s = MD44State()
    s.master_on = bool(raw["switch"])
    s.channels = [bool(raw[f"channe{i + 1}"]) for i in range(MD44_CHANNEL_COUNT)]
    s.timers_enabled = [bool(raw[f"Timer{i + 1}ON"]) for i in range(MD44_CHANNEL_COUNT)]
    s.intervals_days = [int(raw[f"IntervalT{i + 1}"]) for i in range(MD44_CHANNEL_COUNT)]
    s.schedules = [
        _parse_schedule_blob(raw[f"CH{i + 1}SWTime"]) for i in range(MD44_CHANNEL_COUNT)
    ]
    s.cal_switch = bool(raw["CALSW"])
    # The cloud reported this as a localised label ("校准1" for index 0) and
    # the calibration-channel sensor pulls the trailing digit out of it, so
    # keep a 1-based numeric string for identical behaviour.
    s.cal_set = str(int(raw["CALSet"]) + 1)
    s.calib1 = int(raw["Calib1"])
    s.ymd = raw["YMDData"]
    s.hms = raw["HMSData"]
    s.channel_ttl = int(raw["channelTTL"])
    s.time1 = int(raw["time1"])
    s.open_circuit = bool(raw["OpenCircuit"])
    s.fault_uart = bool(raw["Fault_UART"])
    return s


class MD44Device:
    """Local LAN client for the MD-4.4 doser."""

    model = "MD-4.4"

    def __init__(
        self,
        host: str,
        device_id: Optional[str] = None,
        did: Optional[str] = None,
        port: int = TCP_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self.did = did or device_id or host
        self.device_id = device_id or did or host

        self.state: Optional[MD44State] = None
        # Called with the new MD44State on every push. The coordinator wires
        # this up so entities re-render immediately.
        self.on_push_state: Optional[Callable[[MD44State], Any]] = None

        self._p0: Optional[bytes] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._supervisor: Optional[asyncio.Task] = None
        self._keepalive: Optional[asyncio.Task] = None
        self._stopping = False
        self._online = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._status_waiters: List[asyncio.Future] = []
        self._ack_waiters: Dict[int, asyncio.Future] = {}
        self._sn = 0

    # ------------------------------------------------------------ properties

    @property
    def host(self) -> str:
        return self._host

    @host.setter
    def host(self, value: str) -> None:
        """Allow the coordinator to repoint us after a DHCP address change."""
        self._host = value

    @property
    def is_connected(self) -> bool:
        return self._online.is_set()

    @property
    def push_active(self) -> bool:
        """Whether the push channel is live. The pump reports changes on the
        same socket, so this is just connection state."""
        return self._online.is_set()

    # ------------------------------------------------------------- lifecycle

    async def connect(self, timeout: float = 10.0) -> None:
        """Open the connection and fetch initial state."""
        self._stopping = False
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self._supervise(), name=f"jebao_md44-{self._host}"
            )
        try:
            await asyncio.wait_for(self._online.wait(), timeout=timeout)
        except asyncio.TimeoutError as err:
            await self.disconnect()
            raise MD44ConnectionError(
                f"No response from pump at {self._host}:{self._port}"
            ) from err
        await self.update()

    async def disconnect(self) -> None:
        self._stopping = True
        for task_attr in ("_keepalive", "_supervisor"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                    pass
                setattr(self, task_attr, None)
        await self._close_socket()
        self._online.clear()

    async def _close_socket(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # pylint: disable=broad-except
                pass
        self._reader = self._writer = None

    async def _supervise(self) -> None:
        backoff = RECONNECT_MIN
        while not self._stopping:
            try:
                await self._open()
                _LOGGER.info("Connected to MD-4.4 at %s", self._host)
                backoff = RECONNECT_MIN
                self._online.set()
                if self._keepalive is None or self._keepalive.done():
                    self._keepalive = asyncio.create_task(self._keepalive_loop())
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("MD-4.4 connection to %s failed: %s", self._host, err)
            finally:
                self._online.clear()
                self._fail_waiters(MD44ConnectionError("connection lost"))
                await self._close_socket()
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _open(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), timeout=8.0
        )
        # Ask the pump for its passcode, then echo it back to log in. No
        # cloud-issued credential is involved.
        await self._send(proto.build_passcode_request())
        body = await asyncio.wait_for(self._read_body(), timeout=5.0)
        if len(body) < 5 or body[2] != proto.MSG_PASSCODE_RES:
            raise MD44ConnectionError("unexpected passcode response")
        plen = int.from_bytes(body[3:5], "big")
        passcode = body[5:5 + plen]
        if not passcode:
            raise MD44ConnectionError("pump returned an empty passcode")
        await self._send(proto.build_login(passcode))
        body = await asyncio.wait_for(self._read_body(), timeout=5.0)
        if len(body) < 4 or body[2] != proto.MSG_LOGIN_RES or body[3] != 0x00:
            raise MD44ConnectionError("pump rejected login")

    # ------------------------------------------------------------- transport

    async def _send(self, data: bytes) -> None:
        if self._writer is None:
            raise MD44ConnectionError("not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def _read_varint(self) -> int:
        assert self._reader is not None
        val = shift = 0
        for _ in range(5):
            chunk = await self._reader.readexactly(1)
            val |= (chunk[0] & 0x7F) << shift
            if not chunk[0] & 0x80:
                return val
            shift += 7
        raise MD44Error("varint too long")

    async def _read_body(self) -> bytes:
        assert self._reader is not None
        magic = await self._reader.readexactly(4)
        if magic != proto.MAGIC:
            raise MD44Error(f"bad magic {magic.hex()}")
        return await self._reader.readexactly(await self._read_varint())

    async def _read_loop(self) -> None:
        """Consume frames until the socket closes, dispatching as we go."""
        while not self._stopping:
            body = await self._read_body()
            self._dispatch(body)

    def _dispatch(self, body: bytes) -> None:
        if len(body) < 3:
            return
        msg = body[2]
        if msg == proto.MSG_STATUS_RES and len(body) >= 100:
            try:
                p0 = proto.p0_from_status_body(body)
                self._p0 = p0
                self.state = _state_from_raw(proto.decode_p0(p0))
            except (proto.LanProtocolError, KeyError, IndexError) as err:
                _LOGGER.debug("Could not decode status frame: %s", err)
                return
            for fut in self._status_waiters:
                if not fut.done():
                    fut.set_result(self.state)
            self._status_waiters.clear()
            self._notify_push()
        elif msg == proto.MSG_CONTROL_RES and len(body) >= 7:
            sn = int.from_bytes(body[3:7], "big")
            fut = self._ack_waiters.pop(sn, None)
            if fut is not None and not fut.done():
                fut.set_result(body)
        else:
            _LOGGER.debug("Unhandled frame type 0x%02x (%d bytes)", msg, len(body))

    def _notify_push(self) -> None:
        callback = self.on_push_state
        if callback is None or self.state is None:
            return
        try:
            result = callback(self.state)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("push handler raised")

    def _fail_waiters(self, err: Exception) -> None:
        for fut in self._status_waiters:
            if not fut.done():
                fut.set_exception(err)
        self._status_waiters.clear()
        for fut in self._ack_waiters.values():
            if not fut.done():
                fut.set_exception(err)
        self._ack_waiters.clear()

    async def _keepalive_loop(self) -> None:
        """Periodic status request: keeps the socket warm, detects a silently
        dead connection, and refreshes state if a push was ever missed."""
        try:
            while not self._stopping and self._online.is_set():
                await asyncio.sleep(KEEPALIVE_SECONDS)
                try:
                    await self.update()
                except MD44Error as err:
                    _LOGGER.debug("Keepalive poll failed: %s", err)
                    return
        except asyncio.CancelledError:
            return

    # ---------------------------------------------------------------- reads

    async def update(self) -> MD44State:
        """Request a status report and wait for it."""
        if not self._online.is_set():
            raise MD44ConnectionError("not connected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._status_waiters.append(fut)
        try:
            await self._send(proto.build_status_request())
            return await asyncio.wait_for(fut, timeout=STATUS_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise MD44ConnectionError("pump did not report status") from err
        except (OSError, ConnectionError) as err:
            raise MD44ConnectionError(str(err)) from err
        finally:
            if fut in self._status_waiters:
                self._status_waiters.remove(fut)

    # --------------------------------------------------------------- writes

    async def _write(self, changes: Dict[str, Any]) -> None:
        """Send one 0x93 control frame and wait for its ack."""
        if not self._online.is_set():
            raise MD44ConnectionError("not connected")
        async with self._write_lock:
            if self._p0 is None:
                await self.update()
            if self._p0 is None:
                raise MD44ConnectionError("no state to base a write on")
            self._sn = (self._sn + 1) & 0xFFFFFFFF
            sn = self._sn
            try:
                frame = proto.build_control(self._p0, changes, sn)
            except proto.LanProtocolError as err:
                raise MD44Error(str(err)) from err

            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._ack_waiters[sn] = fut
            try:
                await self._send(frame)
                await asyncio.wait_for(fut, timeout=ACK_TIMEOUT)
            except asyncio.TimeoutError as err:
                raise MD44Error("pump did not acknowledge the write") from err
            except (OSError, ConnectionError) as err:
                raise MD44ConnectionError(str(err)) from err
            finally:
                self._ack_waiters.pop(sn, None)

        # The pump pushes a fresh status report after applying a change; ask
        # for one anyway so callers can rely on state being current.
        try:
            await self.update()
        except MD44Error:
            pass

    async def set_master(self, on: bool) -> None:
        _LOGGER.info("MD-4.4: master switch -> %s", on)
        await self._write({"switch": 1 if on else 0})

    async def set_channel(self, idx: int, on: bool) -> None:
        if not 0 <= idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"channel idx out of range: {idx}")
        _LOGGER.info("MD-4.4: channel %d -> %s", idx + 1, on)
        await self._write({f"channe{idx + 1}": 1 if on else 0})

    async def set_timer_enabled(self, idx: int, on: bool) -> None:
        if not 0 <= idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"timer idx out of range: {idx}")
        _LOGGER.info("MD-4.4: timer %d enabled -> %s", idx + 1, on)
        await self._write({f"Timer{idx + 1}ON": 1 if on else 0})

    async def set_interval_days(self, idx: int, days: int) -> None:
        if not 0 <= idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"interval idx out of range: {idx}")
        if not 0 <= days <= 255:
            raise ValueError(f"interval days out of range: {days}")
        _LOGGER.info("MD-4.4: IntervalT%d -> %d days", idx + 1, days)
        await self._write({f"IntervalT{idx + 1}": int(days)})

    async def set_schedule(
        self, channel_idx: int, entries: List[ScheduleEntry]
    ) -> None:
        """Overwrite one channel's schedule. Entries are written in order
        from slot 1; remaining slots up to 24 are zeroed."""
        if not 0 <= channel_idx < MD44_CHANNEL_COUNT:
            raise ValueError(f"channel idx out of range: {channel_idx}")
        if len(entries) > proto.SCHEDULES_PER_CHANNEL:
            raise ValueError(
                f"max {proto.SCHEDULES_PER_CHANNEL} schedule entries per channel"
            )
        attr = f"CH{channel_idx + 1}SWTime"
        _LOGGER.info("MD-4.4: writing %d schedule entries to %s", len(entries), attr)
        await self._write({attr: _schedule_to_blob(entries)})

    async def sync_time(self, now: Optional[dt.datetime] = None) -> None:
        """Set the pump's clock to local wall-clock time.

        Encoding, established by reading back what the official app had
        written and confirming the pump retains our bytes verbatim::

            YMDData = [20, YY, MM, DD]   # byte 0 is the century, not year-2000
            HMSData = [ 0, HH, MM, SS]   # byte 0 is reserved, NOT a weekday

        Byte 0 of HMSData is not day-of-week: the app had written 0 for a
        Wednesday. Day-of-week is not part of the schedule model anyway
        (entries are hour/minute plus an interval-in-days), so nothing
        depends on it.

        These two datapoints are a write-only latch — they hold whatever
        was last written and never advance, so the pump's running clock
        cannot be read back. That means a successful write cannot be
        verified directly beyond confirming the bytes were stored.
        """
        if now is None:
            now = dt.datetime.now()
        ymd = bytes([20, now.year - 2000, now.month, now.day]).hex()
        hms = bytes([0, now.hour, now.minute, now.second]).hex()
        _LOGGER.info("MD-4.4: setting clock to %s", now.isoformat(timespec="seconds"))
        await self._write({"YMDData": ymd, "HMSData": hms})
