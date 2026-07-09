# v0.3.0 — Auxiliary loads, configurable source temp, coordinator persistence

## Motivation
The previous power model (compressor via measured thermal power / dynamic
COP + DHW heater + a single lump passive-cooling estimate) never accounted
for the always-on standby draw or the brine/heating circulation pumps
outside of passive cooling, and had no restart-survival path for the
status/modulation datapoints that drive those calculations — a real risk for
the Energy dashboard given CAN only re-broadcasts a datapoint on change.

## Changes

### New configurable options (Options flow, same validated-schema pattern as
the existing heater/cooling power fields)
- `source_temp_c` (default 12.5 °C) — ground-loop temperature feeding the COP
  lift calculation. No CAN datapoint exists for this on this installation
  (confirmed: the Rücklauf/Vorlauf Erdsonde gauges are analog-only); this
  replaces the previously hardcoded `COP_SOURCE_TEMP` constant.
- `brine_pump_power_w` (default 30 W) — ground-loop circulation pump.
  Confirmed via Hoval's own spec sheet that this is the same class of
  "drehzahlregulierte Hocheffizienzpumpe" as the heating-circuit pump, not a
  separate high-draw unit — default set accordingly (previously assumed at
  220 W in the reference script; corrected after review).
- `heating_pump_power_w` (default 20 W) — heating-circuit pump, median of its
  own nameplate's 4-40 W dynamic range.
- `standby_power_w` (default 12 W) — TopTronic E controller + Siemens
  GLB341.9E valve actuator (nameplate 1.9 W/5.8 VA) idle draw. Always added,
  independent of heat-pump/DHW/cooling state.

### New entities
- Brine Pump Power, Heating Pump Power, Standby Power (all kW). Brine/heating
  pump power sensors are active whenever `coordinator.pumps_active` is true —
  compressor active (heating/DHW) OR passive/free cooling, since both draw
  the ground loop and heating circuit pumps.

### Total Electrical Power/Energy
Now: compressor (unchanged: measured thermal power / dynamic COP) + DHW
heater (unchanged) + brine pump + heating pump + standby. The previous
`cooling_power_kw` lump estimate is no longer part of the total (it modelled
the same physical pumps as the new brine+heating pump terms; keeping both
would double-count). Its own entity is retained, unconditionally, purely for
history continuity — not fed into Total Power/Energy anymore.

Standby is never zero-filled, so Total Electrical Power always reports at
least the standby floor rather than 0 whenever the gateway is connected —
closing the "totals unknown/zero forever" gap for periods with no other load.

### Coordinator-level persistence (Store-backed)
`PERSISTENT_DPIDS` extended from `{23009}` to also include the status/
modulation/temperature datapoints that feed `cop`, `electric_heater_on`,
`passive_cooling_on`, and the new `heat_pump_active`/`pumps_active`:
`status_heat_pump`, `status_heating_circuit`, `status_dhw`,
`compressor_modulation`, `heat_gen_temp`, `current_heating_power`,
`dhw_temp`, `dhw_setpoint`.

Two independent restore paths now cover this set:
- Entity-level (`HovalPersistentSensor`, pre-existing pattern): each raw
  sensor restores its own last displayed value.
- Coordinator-level (new, `HovalCANCoordinator._async_load_persisted` /
  `_schedule_persist` / `async_replay_restored_signals`): seeds
  `self._data` from a debounced (30 s) `homeassistant.helpers.storage.Store`
  write *before* the sensor platform is set up, then replays dispatcher
  signals for the restored datapoints once entities are subscribed — this is
  what actually lets Total Electrical Power resolve to a real number
  immediately after a restart instead of waiting for CAN to re-broadcast a
  value that hasn't changed.

Fresh CAN data always overwrites a restored value (already-established
philosophy elsewhere in this integration); a corrupt or missing store degrades
to today's cold-start behaviour rather than blocking startup.

### Tests
`tests/test_protocol.py`: added a functional in-memory `Store` stub (real
round-trip, not just an import shim) and three new test groups —
`test_power_model_options` (option defaults/overrides/garbage/negative
clamping, `calculate_cop` with a custom `source_temp`, `heat_pump_active` /
`pumps_active` transitions), `test_persistence` (save → simulated restart →
load → signal replay → live-data overwrite → corrupt-store fallback), and
updated `test_cooling_sensors`'s `FakeCoord`/assertions for the new Total
Power formula. Also fixed one pre-existing stale assertion in
`test_cooling_sensors` ("totals Unknown when thermal/heater unknown") that
already failed against the unmodified v0.2.8 codebase — it contradicted the
sensor's own documented zero-fill behaviour and was unrelated to this change.

Suite: all assertions pass; `pyflakes` clean across all modules.
