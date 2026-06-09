"""Hoval CAN coordinator: manages TCP connection and frame decoding."""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CMD_READ_RESP_BE, CMD_READ_RESP_LE,
    CONF_HEATER_POWER, DEFAULT_HEATER_POWER_KW, DEFAULT_PORT,
    DOMAIN, FRAME_END, FRAME_SEP, FRAME_TIMEOUT,
    HEATER_DETECTION_MARGIN, NULL_SENTINELS, RECONNECT_DELAY,
    SCHEDULE_GROUPS, SENSOR_BY_DPID, STRUCT_FMT, TBYTES,
    DP_DHW_ACTUAL, DP_DHW_SETPOINT, DP_STATUS_WW,
    DP_HEAT_GEN, DP_MODULATION, DHW_STATUS_CHARGING,
    calculate_cop,
    connection_signal, dp_signal, heater_signal,
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
    def connected(self) -> bool:
        return self._connected

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

    def get_value(self, dp_id: int) -> Any:
        return self._data.get(dp_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        self._stop = False
        self._task = self.hass.loop.create_task(
            self._connection_loop(),
            name=f"hoval_can_{self._entry.entry_id}",
        )

    async def async_stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Connection loop ───────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        while not self._stop:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                if not self._stop:
                    _LOGGER.warning(
                        "Hoval CAN: connection to %s failed: %s — retry in %ds",
                        self._host, exc, RECONNECT_DELAY,
                    )
                    self._set_connected(False)
                    await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_read(self) -> None:
        _LOGGER.info("Hoval CAN: connecting to %s:%d …", self._host, self._port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), timeout=10
        )
        self._set_connected(True)
        _LOGGER.info("Hoval CAN: connected.")
        buf = b""
        try:
            while not self._stop:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=float(FRAME_TIMEOUT)
                    )
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    raise ConnectionError("Device closed connection")
                buf += chunk
                parts = buf.split(FRAME_SEP)
                buf = parts[-1]
                for part in parts[:-1]:
                    self._handle_frame(part)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._set_connected(False)

    # ── Frame handling ────────────────────────────────────────────────────

    def _handle_frame(self, data: bytes) -> None:
        s = data[:-2] if data.endswith(FRAME_END) else data
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
        async_dispatcher_send(self.hass, dp_signal(self._entry.entry_id, dp_id))

        if dp_id in (DP_STATUS_WW, DP_DHW_ACTUAL, DP_DHW_SETPOINT, DP_HEAT_GEN):
            new_state = self.electric_heater_on
            if new_state != self._heater_on:
                self._heater_on = new_state
                async_dispatcher_send(
                    self.hass, heater_signal(self._entry.entry_id)
                )

    def _set_connected(self, state: bool) -> None:
        if state != self._connected:
            self._connected = state
            async_dispatcher_send(
                self.hass, connection_signal(self._entry.entry_id)
            )


# ── Numeric decoder (module-level for clarity) ────────────────────────────

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
