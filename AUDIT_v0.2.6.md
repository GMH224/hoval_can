# Hoval CAN — Full Audit, ICS Deployment Quality (v0.2.6)

Cumulative audit across v0.2.1 → v0.2.6. Focus: failure modes, resource
exhaustion, data integrity under network corruption and clock anomalies,
concurrency/lifecycle, and malformed-input handling. The integration is and
remains strictly **read-only** (no `writer.write`/`drain` on any path; the TCP
writer is only ever closed). Every fix is exercised by executing the real code
(`tests/test_protocol.py`, runnable without Home Assistant).

## Severity legend
P1 = can stop data collection or corrupt stored data · P2 = degraded
reliability/observability · P3 = robustness/style.

## Defect register (all resolved)

| # | Sev | Component | Finding | Resolution | Ver |
|---|-----|-----------|---------|------------|-----|
| 1 | **P1** | read loop | Silent stall on half-open socket: `read()` timeout looped forever, `connected` stayed True, all sensors froze. Root cause of the ~3 h outage. | Byte-level watchdog (90 s) → reconnect. | 0.2.2 |
| 2 | **P1** | read loop | Live-but-desynced stream (bytes flow, nothing decodable) not caught by byte watchdog. | Data-level watchdog (300 s on last decode). | 0.2.2 |
| 3 | **P1** | energy sensors | False kWh spike after reconnect: integrators integrated the whole downtime as one lump. | Discard open interval on disconnect. | 0.2.2 |
| 4 | **P1** | energy sensors | Wall-clock integration corrupts totals on NTP/DST steps. | Monotonic clock for all elapsed time. | 0.2.2 |
| 5 | **P1** | sensor.py | Regression introduced mid-audit: dropped `_update` header (undefined names). | Restored; caught by re-audit + pyflakes + tests. | 0.2.2 |
| 6 | **P1** | RX buffer | Unbounded buffer growth if separator never appears → memory exhaustion. | 64 KiB cap + resync. | 0.2.2 |
| 7 | P2 | socket | No TCP keep-alive → ~2 h kernel default to notice a dead peer. | `SO_KEEPALIVE` + tuned timers. | 0.2.2 |
| 8 | P2 | reconnect | Fixed-interval retry + full-warning every attempt → storm/log flood. | Capped backoff + jitter; log de-dup. | 0.2.2 |
| 9 | P2 | lifecycle | Untracked `loop.create_task` → leak / shutdown warning. | `entry.async_create_background_task`. | 0.2.2 |
| 10 | P2 | observability | No signal for a freeze. | Diagnostic connectivity sensor (age, reconnects, last error). | 0.2.2 |
| 11 | **P1** | frame parser | **Value byte-pair equal to a frame marker (`FF 01`/`FF 02`) could mis-frame data**, corrupting that sample and sometimes the next. Split-on-start-marker parsing. | **Length-aware parser**: known fixed-width value length + verified `FF 02` position; STR/unknown fall back to end-marker scan; misplaced end-marker → counted desync + resync. Monitored sensors never updated from a mis-delimited frame. | 0.2.3 |
| 12 | **P1** | frame parser | Regression while building #11: post-parse code still trimmed a trailing `FF 02`, corrupting any value ending in `FF 02`. | Removed redundant trim (parser already delimits). Caught by the value-ends-`FF 02` test. | 0.2.3 |
| 13 | P2 | observability | Frame desync not visible. | `framing_errors` counter on the connectivity sensor. | 0.2.3 |

## Reviewed, acceptable (no change)
- **Index/length safety**: every slice and `struct.unpack` is length-guarded;
  malformed input returns early. STR decode uses `latin-1` + printable filter.
- **COP math**: division guarded (`t ≤ source → 0.0`); result clamped
  `[1.0, 8.5]`; reproduces documented reference points.
- **Concurrency**: all state on the single HA event loop; reader task runs on
  the loop → race-free `_data`/dispatcher access without locks.
- **Energy `_tick`** updates only the live display, never the running total →
  no double counting vs the event path.
- **Config flow**: port range-checked (1–65535); host stripped; unique-id
  `host:port` prevents duplicates.

## Residual / known limitations (documented, fail-safe)
- **Value-field offset assumption**: the length-aware parser assumes the value
  sits at byte 10 immediately before `FF 02` (per the project's documented
  layout, and consistent with the prior working decoder). If the device emits
  undocumented trailing bytes, `framing_errors` rises and the data watchdog
  forces reconnects — observable and self-recovering, not silent. Re-confirm
  against a live capture if `framing_errors` is non-trivial in the field.
- **Unknown/STR frames** use end-marker scanning; an in-value `FF 02` there can
  truncate that (unmonitored) frame and cost the *following* sample a resync.
  Monitored fixed-width sensors are unaffected by construction.
- `0x70` PUSH-MULTI and `0x56` write frames remain uncharacterised (skipped).

## Verification (executed — `tests/test_protocol.py`, ALL PASS)
- COP: SH t=30 m∈{12}→7.0, m=33→6.51, m=50→5.74; DHW t=52 m=33→3.25,
  m=100→2.50; guard-rails + clamp.
- Decode: S16/U16/U32, every null sentinel → None, all-FF → None, short → None,
  little-endian.
- **Framing (adversarial)**: in-value `FF 01`; in-value `FF 02`; value ending
  in `FF 02`; frame split across reads; garbage→resync; LE room-display; STR
  variable length; schedule-group skip; RX cap. Each asserts the monitored
  value is correct *and* the following frame is intact.
- **Watchdog (real loop)**: fake half-open socket raises within the threshold
  (no hang); peer-closed raises immediately.
- Integrator: 1 h @ 1 kW → 1.000 kWh; backward-time guard adds 0.
- `py_compile` + `pyflakes`: clean across all modules.

## Feature additions — ICS review

### v0.2.4 Passive cooling energy (DpId 2051 == 9)
- **No double-count**: passive cooling runs with the compressor off, so the
  COP-based heat-pump term is 0; the cooling term is purely additive. Verified.
- **No regression**: unknown/absent cooling status is treated as 0 W and does
  not gate availability, so installs without a cooling circuit are unaffected.
- Reuses the audited energy machinery (monotonic clock, open-interval discard on
  disconnect, RestoreEntity). Config power validated/coerced (W→kW, negative→0).
- Tests: status detection, edge-trigger (exactly one per transition), combined
  power formula, no-regression rule, 1 h @ 100 W → 0.100 kWh.

### v0.2.5 Diagnostics / telemetry
- Health counters promoted from binary-sensor attributes to first-class
  diagnostic sensors (data age, reconnects, framing errors, decoded count) so
  they are recorded, graphable, alarmable, and exporter-visible. Polled model
  (continuously changing / no-signal counters); available while disconnected.
- Downloadable config-entry diagnostics with host/IP and unique_id redaction;
  graceful when the coordinator is not loaded.
- Read-only and side-effect-free: diagnostics only read coordinator state.
- Tests: snapshot structure + named last-values, redaction, missing-coordinator
  path, and each sensor's value wiring. Suite total: 68 assertions, all pass.

### v0.2.6 Windowed health rates
- Sliding-window throughput (decoded/min, 60-min) and framing-error rate
  (errors/h, 15-min) from a bounded 60-s snapshot ring (≈60 samples, maxlen-
  capped + age-pruned → no unbounded growth). Leading edge uses live counters,
  so a stalled stream decays the rate to 0 rather than freezing it.
- The sampler timer is registered in `async_start` and **unsubscribed in
  `async_stop`** (no leaked callback across reload/unload). Pure rate function
  is side-effect-free and unit-tested for empty/warm-up/expired/flat/normal
  cases; properties verified with a deterministic clock.
- Errors-per-decoded ratio intentionally omitted (denominator collapses on
  stream stall; redundant for a near-constant-cadence device). Rationale and
  the zero-code HA-helper alternative documented in the README.
- Suite total: 86 assertions, all pass.

## Deployment guidance (operator)
- Alarm on `binary_sensor.hoval_can_gateway_connection` = off beyond a short
  grace, or on the new `sensor.hoval_can_gateway_data_age` exceeding your
  tolerance, or a rising `sensor.hoval_can_gateway_framing_errors`.
- For incident triage, use Download Diagnostics (host is redacted).
- Site constants live in `const.py` (watchdog timeouts, backoff, keep-alive,
  RX cap).
- This is read-only monitoring telemetry; it is not a safety-rated control
  function. Run Home Assistant itself under process supervision and treat this
  single-source data accordingly.
