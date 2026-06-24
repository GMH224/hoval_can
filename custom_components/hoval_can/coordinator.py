"""Hoval CAN coordinator: manages TCP connection and frame decoding."""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CMD_READ_RESP_BE, CMD_READ_RESP_LE,
    CONF_HEATER_POWER, DEFAULT_HEATER_POWER_KW, DEFAULT_PORT,
    CONF_COOLING_POWER, DEFAULT_COOLING_POWER_W,
    FRAME_END, FRAME_SEP, FRAME_TIMEOUT, STALE_TIMEOUT,
    DATA_STALE_TIMEOUT, MAX_RX_BUFFER, RX_RESYNC_KEEP,
    HEATER_DETECTION_MARGIN, NULL_SENTINELS,
    RECONNECT_DELAY, RECONNECT_DELAY_MAX, RECONNECT_BACKOFF,
    SCHEDULE_GROUPS, SENSOR_BY_DPID, STRUCT_FMT, TBYTES,
    TCP_KEEPALIVE_IDLE, TCP_KEEPALIVE_INTERVAL, TCP_KEEPALIVE_COUNT,
    DP_DHW_ACTUAL, DP_DHW_SETPOINT, DP_STATUS_WW,
    DP_STATUS_HC, HC_STATUS_PASSIVE_COOLING,
    DP_HEAT_GEN, DP_MODULATION, DHW_STATUS_CHARGING,
    calculate_cop,
    connection_signal, cooling_signal, dp_signal, heater_signal,
)

_LOGGER = logging.getLogger(__name__)


class HovalCANCoordinator:
    """Manages the TCP connection to the Hoval WLAN gateway.

    Parses incoming CAN-BUS frames and dispatches HA dispatcher signals.
    Strictly read-only — nothing is ever written to the bus.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass        = hass
        self._entry      = entry
        self._host: str  = entry.data["host"]
        self._port: int  = entry.data.get("port", DEFAULT_PORT)
        self._data: dict[int, Any] = {}
        self._connected  = False
        self._task: asyncio.Task | None = None
        self._stop       = False
        self._heater_on: bool | None = None
        self._cooling_on: bool | None = None
        # ── Observability / diagnostics (read by the connectivity sensor) ──
        self._last_data_mono: float = 0.0   # monotonic ts of last decoded dp
        self._reconnect_count: int  = 0     # successful (re)connects after 1st
        self._last_error: str | None = None # last connection failure reason
        self._framing_errors: int   = 0     # cumulative frame desync events
        self._decoded_count: int    = 0     # cumulative decoded datapoints

    # ── Public properties ─────────────────────────────────────────────────

    @property
    def cop(self) -> float:
        """Dynamic COP from live modulation and heat-generator temperature.

        Two-regime piecewise model (see const.calculate_cop for details):
          • T_gen ≤ 40 °C → space heating regime
          • T_gen >  40 °C → DHW regime
        Returns 0.0 when the heat pump is not running.
        """
        m = float(self._data.get(DP_MODULATION) or 0.0)
        t = float(self._data.get(DP_HEAT_GEN)   or 0.0)
        return calculate_cop(m, t)

    @property
    def heater_power_kw(self) -> float:
        """Rated power of the electric DHW heater in kW (from options)."""
        try:
            return float(
                self._entry.options.get(CONF_HEATER_POWER, DEFAULT_HEATER_POWER_KW)
            )
        except (TypeError, ValueError):
            return DEFAULT_HEATER_POWER_KW

    @property
    def cooling_power_kw(self) -> float:
        """Passive-cooling circulation power in kW.

        Configured in WATTS (CONF_COOLING_POWER); converted to kW here so it
        composes with the kW-based power/energy maths. Negative/garbage values
        are coerced to a safe default.
        """
        try:
            watts = float(
                self._entry.options.get(CONF_COOLING_POWER, DEFAULT_COOLING_POWER_W)
            )
        except (TypeError, ValueError):
            watts = DEFAULT_COOLING_POWER_W
        return max(0.0, watts) / 1000.0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_data_age(self) -> float | None:
        """Seconds since the last decoded datapoint, or None if never."""
        if self._last_data_mono == 0.0:
            return None
        return max(0.0, time.monotonic() - self._last_data_mono)

    @property
    def reconnect_count(self) -> int:
        """Number of successful reconnects since the integration loaded."""
        return self._reconnect_count

    @property
    def last_error(self) -> str | None:
        """Human-readable reason for the most recent connection failure."""
        return self._last_error

    @property
    def framing_errors(self) -> int:
        """Cumulative count of frame desync events (resyncs)."""
        return self._framing_errors

    @property
    def decoded_count(self) -> int:
        """Cumulative number of datapoints decoded since load (throughput)."""
        return self._decoded_count

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """Structured health + last-seen-data snapshot for downloadable
        config-entry diagnostics. Host/IP is redacted by the caller."""
        named: dict[str, Any] = {}
        for dp_id, value in sorted(self._data.items()):
            desc = SENSOR_BY_DPID.get(dp_id)
            key = desc.key if desc is not None else f"dp_{dp_id}"
            named[key] = value
        return {
            "host": self._host,
            "port": self._port,
            "connection": {
                "connected": self._connected,
                "last_data_age_seconds": (
                    None if self.last_data_age is None
                    else round(self.last_data_age, 1)
                ),
                "reconnect_count": self._reconnect_count,
                "framing_errors": self._framing_errors,
                "decoded_count": self._decoded_count,
                "last_error": self._last_error,
            },
            "options": {
                "heater_power_kw": self.heater_power_kw,
                "cooling_power_w": round(self.cooling_power_kw * 1000.0, 1),
            },
            "derived": {
                "cop": self.cop,
                "electric_heater_on": self.electric_heater_on,
                "passive_cooling_on": self.passive_cooling_on,
            },
            "datapoints_seen": len(self._data),
            "last_values": named,
        }

    @property
    def electric_heater_on(self) -> bool | None:
        """True when the Heizstab is active (derived — no direct DpId).

        ON when all three hold:
          1. DHW charging (status_ww == 8)
          2. DHW below setpoint
          3. Generator ≤ DHW + 5 °C  (heat pump can't heat the tank)

        Winter-safe: heat pump at 40 °C for space heating cannot heat a
        55 °C+ DHW tank; condition 3 fires correctly.
        """
        status_ww  = self._data.get(DP_STATUS_WW)
        dhw_actual = self._data.get(DP_DHW_ACTUAL)
        dhw_sp     = self._data.get(DP_DHW_SETPOINT)
        heat_gen   = self._data.get(DP_HEAT_GEN)
        if None in (status_ww, dhw_actual, dhw_sp, heat_gen):
            return None
        return bool(
            status_ww == DHW_STATUS_CHARGING
            and dhw_actual < dhw_sp
            and heat_gen <= dhw_actual + HEATER_DETECTION_MARGIN
        )

    @property
    def passive_cooling_on(self) -> bool | None:
        """True when the heating circuit is in passive ("free") cooling mode.

        Direct read of Heating Circuit Status (DpId 2051 == 9). Returns None
        until that datapoint has been seen, so consumers can treat 'unknown'
        as 'not cooling' — installations without a cooling circuit therefore
        never regress the electrical totals.

        Passive cooling runs with the compressor OFF (only circulation pumps),
        so this power is additive to — and does not overlap with — the
        COP-based heat-pump electrical term (which is 0 when the compressor is
        idle).
        """
        status = self._data.get(DP_STATUS_HC)
        if status is None:
            return None
        return status == HC_STATUS_PASSIVE_COOLING

    def get_value(self, dp_id: int) -> Any:
        return self._data.get(dp_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        self._stop = False
        # Use the config-entry's tracked background-task helper where available
        # (HA 2023.4+) so the task is owned by the entry and is reliably
        # cancelled on unload / HA shutdown. Fall back to a raw loop task on
        # older cores. A bare loop.create_task() would otherwise leak and emit
        # "Task was destroyed but it is pending" on shutdown.
        coro = self._connection_loop()
        name = f"hoval_can_{self._entry.entry_id}"
        if hasattr(self._entry, "async_create_background_task"):
            self._task = self._entry.async_create_background_task(
                self.hass, coro, name
            )
        else:  # pragma: no cover - legacy cores
            self._task = self.hass.loop.create_task(coro, name=name)

    async def async_stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ── Connection loop ───────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        delay = RECONNECT_DELAY
        while not self._stop:
            try:
                await self._connect_and_read()
                # Clean return (only on stop) — no backoff needed.
                delay = RECONNECT_DELAY
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                if self._stop:
                    return
                self._set_connected(False)
                reason = f"{type(exc).__name__}: {exc}"
                # Log the first failure of an outage at WARNING; demote the
                # repeats to DEBUG so a long outage cannot flood the log, then
                # announce recovery once we reconnect.
                if reason != self._last_error:
                    _LOGGER.warning(
                        "Hoval CAN: connection to %s lost (%s) — retrying "
                        "(backoff up to %ds)", self._host, reason,
                        RECONNECT_DELAY_MAX,
                    )
                else:
                    _LOGGER.debug(
                        "Hoval CAN: still down (%s) — retry in %.0fs",
                        reason, delay,
                    )
                self._last_error = reason
                # Exponential backoff with full jitter, capped.
                sleep_for = min(delay, RECONNECT_DELAY_MAX)
                sleep_for = random.uniform(RECONNECT_DELAY, sleep_for) \
                    if sleep_for > RECONNECT_DELAY else RECONNECT_DELAY
                await asyncio.sleep(sleep_for)
                delay = min(delay * RECONNECT_BACKOFF, RECONNECT_DELAY_MAX)

    async def _connect_and_read(self) -> None:
        _LOGGER.info("Hoval CAN: connecting to %s:%d …", self._host, self._port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), timeout=10
        )
        _enable_keepalive(writer)
        was_down = self._last_error is not None
        self._last_error = None
        self._last_data_mono = time.monotonic()  # grace period from connect
        if was_down:
            self._reconnect_count += 1
            _LOGGER.info(
                "Hoval CAN: reconnected to %s (reconnect #%d).",
                self._host, self._reconnect_count,
            )
        else:
            _LOGGER.info("Hoval CAN: connected.")
        self._set_connected(True)
        buf = b""
        last_rx = time.monotonic()   # byte-level watchdog
        try:
            while not self._stop:
                now = time.monotonic()
                # Data-level watchdog: socket alive and maybe even streaming
                # bytes, but no decodable datapoint for DATA_STALE_TIMEOUT
                # (corruption / protocol desync). Bytes alone would fool the
                # byte-level watchdog below, so this guard runs every loop.
                if now - self._last_data_mono >= DATA_STALE_TIMEOUT:
                    raise ConnectionError(
                        f"No decodable data for {DATA_STALE_TIMEOUT}s "
                        "— stream desynced"
                    )
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=float(FRAME_TIMEOUT)
                    )
                except asyncio.TimeoutError:
                    # Byte-level watchdog: a read timeout is NOT normal here
                    # (frames arrive ~2 s apart). Prolonged silence means a
                    # half-open socket (gateway reboot / Wi-Fi drop with no
                    # FIN/RST). Force a reconnect rather than spin forever and
                    # silently freeze every sensor.
                    if time.monotonic() - last_rx >= STALE_TIMEOUT:
                        raise ConnectionError(
                            f"No bytes for {STALE_TIMEOUT}s — socket half-open"
                        )
                    continue
                if not chunk:
                    raise ConnectionError("Device closed connection")
                last_rx = time.monotonic()
                buf += chunk
                buf = self._consume_frames(buf)
                # RX buffer hard cap: if no frame can be extracted the tail
                # must not grow without bound (memory-exhaustion guard).
                if len(buf) > MAX_RX_BUFFER:
                    _LOGGER.warning(
                        "Hoval CAN: RX buffer exceeded %d bytes without a valid "
                        "frame — discarding to resync.", MAX_RX_BUFFER,
                    )
                    buf = buf[-RX_RESYNC_KEEP:]
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._set_connected(False)

    # ── Frame handling ────────────────────────────────────────────────────

    # Fixed header geometry of a frame body (the bytes between FRAME_SEP and
    # FRAME_END):  [3B hdr][2B unit][1B cmd][2B group][2B dp_id][value]
    _HDR_LEN = 10   # bytes before the value field

    def _value_field_len(self, cmd: int, dp_id: int) -> int | None:
        """Byte length of the value field for a frame, or None if unknown.

        Known fixed-width frames can be parsed by length, which lets us verify
        the FF 02 end-marker lands exactly where expected — so a value byte
        equal to FF 01 / FF 02 can never mis-frame a monitored datapoint.
        Returns None for variable-length (STR) or unmapped datapoints, which
        fall back to end-marker scanning.
        """
        if cmd == CMD_READ_RESP_LE:
            return 3            # 1 skipped byte (0x00) + 2 data bytes
        if cmd == CMD_READ_RESP_BE:
            desc = SENSOR_BY_DPID.get(dp_id)
            if desc is None or desc.typename == "STR":
                return None
            return TBYTES.get(desc.typename)
        return None

    def _consume_frames(self, buf: bytes) -> bytes:
        """Extract and dispatch every complete frame in ``buf``.

        Returns the unconsumed remainder (to be prepended to the next read).
        Length-aware for known fixed-width datapoints (FF 02 position is
        validated); end-marker scanning for variable/unknown frames. A frame
        whose end-marker is not where the length predicts is treated as a
        desync: we count it and resync to the next start marker. This is the
        ICS-critical property — a monitored sensor is never updated from a
        mis-delimited frame; at worst a sample is dropped.
        """
        i = 0
        n = len(buf)
        seplen = len(FRAME_SEP)
        endlen = len(FRAME_END)
        while True:
            start = buf.find(FRAME_SEP, i)
            if start < 0:
                # No start marker left. Keep a short tail in case a marker is
                # split across reads.
                return buf[max(i, n - (seplen - 1)):]
            content = start + seplen
            if n - content < self._HDR_LEN:
                return buf[start:]          # header incomplete — wait for more

            cmd   = buf[content + 5]
            dp_id = struct.unpack(">H", buf[content + 8:content + 10])[0]
            vlen  = self._value_field_len(cmd, dp_id)

            if vlen is not None:
                end_pos = content + self._HDR_LEN + vlen
                if n - end_pos < endlen:
                    return buf[start:]      # value/end-marker incomplete — wait
                if buf[end_pos:end_pos + endlen] == FRAME_END:
                    self._handle_frame(buf[content:end_pos])
                    i = end_pos + endlen
                    continue
                # End-marker absent where the length predicts → desync.
                self._framing_errors += 1
                i = content                 # resync: search past this marker
                continue

            # Unknown / variable-length: scan for the end marker.
            end = buf.find(FRAME_END, content + self._HDR_LEN)
            if end < 0:
                return buf[start:]          # incomplete — wait for more
            self._handle_frame(buf[content:end])
            i = end + endlen

    def _handle_frame(self, data: bytes) -> None:
        # ``data`` is one frame body (header + value), already delimited and
        # stripped of the FF 01 / FF 02 markers by _consume_frames. It must NOT
        # be trimmed again here: a value field may legitimately end in 0xFF 0x02
        # and trimming would corrupt it.
        s = data
        if len(s) < 10:
            return

        cmd   = s[5]
        group = struct.unpack(">H", s[6:8])[0]
        dp_id = struct.unpack(">H", s[8:10])[0]
        val   = s[10:]

        if cmd == CMD_READ_RESP_LE:
            if not (group & 0x8000):
                return
            if len(val) < 3 or val[0] != 0x00:
                return
            val = val[1:3]
            little_endian = True
        elif cmd == CMD_READ_RESP_BE:
            if group in SCHEDULE_GROUPS:
                return
            little_endian = False
        else:
            return

        if 64000 <= dp_id <= 64050:
            return

        desc = SENSOR_BY_DPID.get(dp_id)
        if desc is None:
            return

        if desc.typename == "STR":
            try:
                txt = val.decode("latin-1")
                if len(txt) >= 1 and all(32 <= ord(c) < 127 for c in txt):
                    self._update_dp(dp_id, txt)
            except (UnicodeDecodeError, AttributeError):
                pass
            return

        value = _decode_numeric(val, desc.typename, desc.decimal, little_endian)
        if value is not None:
            self._update_dp(dp_id, value)

    def _update_dp(self, dp_id: int, value: Any) -> None:
        self._data[dp_id] = value
        self._last_data_mono = time.monotonic()   # feeds the data watchdog
        self._decoded_count += 1
        async_dispatcher_send(self.hass, dp_signal(self._entry.entry_id, dp_id))

        if dp_id in (DP_STATUS_WW, DP_DHW_ACTUAL, DP_DHW_SETPOINT, DP_HEAT_GEN):
            new_state = self.electric_heater_on
            if new_state != self._heater_on:
                self._heater_on = new_state
                async_dispatcher_send(
                    self.hass, heater_signal(self._entry.entry_id)
                )

        if dp_id == DP_STATUS_HC:
            new_cooling = self.passive_cooling_on
            if new_cooling != self._cooling_on:
                self._cooling_on = new_cooling
                async_dispatcher_send(
                    self.hass, cooling_signal(self._entry.entry_id)
                )

    def _set_connected(self, state: bool) -> None:
        if state != self._connected:
            self._connected = state
            async_dispatcher_send(
                self.hass, connection_signal(self._entry.entry_id)
            )


# ── Numeric decoder (module-level for clarity) ────────────────────────────

def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
    """Enable (and, on Linux, tune) TCP keep-alive on the open socket.

    Defense in depth alongside the application-level watchdog: lets the OS
    detect a vanished peer instead of relying on the ~2 h kernel default.
    Silently degrades on platforms that lack the per-socket tuning options.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for opt_name, value in (
        ("TCP_KEEPIDLE", TCP_KEEPALIVE_IDLE),
        ("TCP_KEEPINTVL", TCP_KEEPALIVE_INTERVAL),
        ("TCP_KEEPCNT", TCP_KEEPALIVE_COUNT),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass


def _decode_numeric(
    raw: bytes, typename: str, decimal: int, little_endian: bool = False
) -> float | int | None:
    nb = TBYTES.get(typename, 0)
    if nb == 0 or len(raw) < nb:
        return None
    raw = raw[:nb]
    if raw == bytes([0xFF] * nb):
        return None
    fmt = STRUCT_FMT.get(typename)
    if fmt is None:
        return None
    v = struct.unpack(("<" if little_endian else ">") + fmt, raw)[0]
    if v in NULL_SENTINELS.get(typename, set()):
        return None
    return round(v / 10**decimal, decimal) if decimal > 0 else v
