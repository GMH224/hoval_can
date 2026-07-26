# Hoval CAN — Audit, ICS Deployment Quality (v0.3.3)

Scope: a single defect fix — cold-start / stale-restore contamination of the
health index — plus the accounting change it requires (unobserved time), one
new public coordinator accessor, two new attributes, and the corresponding
tests and documentation. **No statistical model change**: baselines,
z-scores, Σ, T², thresholds, the confidence metric and all entity semantics
are byte-for-byte the same as v0.3.2. The integration remains read-only. The
existing energy sensors and their persistence behaviour are untouched.

Test evidence: `tests/test_protocol.py` (162 checks) and
`tests/test_health.py` (78 checks), both green, both runnable without Home
Assistant.

## Correction to AUDIT_v0.3.2.md

The v0.3.2 audit stated, under "Reviewed, acceptable":

> "Stale-restore integration window (known gap #9) remains untouched per the
> standing owner decision; the health model is additionally defended against
> it by its own gap cap."

**That second clause was wrong**, and it is the reason this defect shipped.
The gap cap (`HEALTH_MAX_GAP_S`, 900 s) bounds the interval between two
*consecutive samples*. It fires exactly once after a resumption — on the
first tick, whose predecessor lies before the outage. From the second tick
onward the sampler is back to its regular 5-minute cadence and the cap never
fires again, while the underlying values may still be store-seeded for up to
an hour. The claim confused "the gap is not integrated" (true) with "stale
values are not integrated" (false). Documented here rather than silently
amended.

## Motivation — the defect

`PERSISTENT_DPIDS` seeds `self._data` from the Store at startup so the
derived power sensors resolve immediately rather than showing `unknown` for
minutes. That is correct and deliberate for *instantaneous* consumers. It is
actively harmful for an *integrating* consumer, and the health sampler is
one.

`get_value()` cannot distinguish a seeded value from a live one — by design.
`HealthTracker._tick()` (v0.3.2) therefore read all ten model inputs and
built a `Sample` from whatever was in `self._data`, with no staleness check.
Eight of the ten inputs are in `PERSISTENT_DPIDS`.

Worked failure, using this installation's own numbers: HA stops at 14:00
with the compressor space-heating at 40 % / 3.5 kW; the machine actually
stops at 14:30; HA returns at 16:00.

- Tick 16:05 — `classify_mode` reads the seeded modulation 40 > 1 and
  returns `MODE_SH` (DpId 502 is not persisted, so the program gate returns
  None and never blocks). The gap cap zeroes this one interval. Nothing
  integrated. ✔
- Tick 16:10 — dt = 300 s, an ordinary interval. Modulation still seeded at
  40, thermal power still seeded at 3.5 kW. `mode_s[SH] += 300` and
  `thermal_kwh += 0.29 kWh` **of heat that was never produced**.
- Repeats every five minutes until a live 20052/29051 frame arrives.

**Why this specifically attacks the model rather than merely adding noise:**
the contamination is asymmetric across the two fused features. Δ2080 and
Δ23009 are device-side counters — the heat pump counts whether or not HA is
listening — so CycleRate survives an outage *correctly*. Only the thermal
integral is corrupted. PF = thermal ÷ electrical, so **η is corrupted while
the cycle rate stays clean**: normal `z_cycle`, extreme `z_eta`. That is
precisely the decoupled signature Hotelling's T² exists to flag. A restart
artifact was able to imitate a compressor fault.

The sign depends on machine state at shutdown, so both error directions are
reachable:

| At shutdown | During downtime | Effect |
|---|---|---|
| Running | Machine stopped | Phantom heat → PF ↑ → η ↑ → **false reassurance**, masks real degradation |
| Idle | Machine ran | Heat missed, Δ23009 still counts it → PF ↓ → η ↓ → **false `elevated`/`high`** |

A second, independent path: `elec_last` could be set from a seeded value.
DpId 23009 only broadcasts on a 1 kWh change, and this installation's own
history shows that is every **1–2 days in summer** — so a day could close
with a stale endpoint, understating Δelec and overstating PF.

**Why no existing guard caught it:** `connected` is True (frames *are*
flowing, just not for these dpids); the ≥12 h coverage rule counts stale
samples toward coverage, making it actively counterproductive; the purity
rule sees clean seeded statuses; the η plausibility band is far too wide to
notice one contaminated hour in an 18-hour day. The only partial mitigation
was accidental — DpId 2 (flow temp) is *not* persisted, so no Carnot samples
accrue while blind, which limits but does not prevent the damage.

## Changes

### 1. `coordinator.is_restored(dp_id)` (new public accessor)
Thin, documented accessor over the pre-existing `_restored_dpids` set. That
set was already correct and self-clearing: `_update_dp` discards a dp_id the
moment a live frame arrives for it (coordinator.py, unchanged). Nothing
about the persistence mechanism was modified — v0.3.3 only *exposes* what
the coordinator already knew. Returns False for any dp_id outside
`PERSISTENT_DPIDS`.

### 2. Blind samples integrate nothing
`Sample` gains `blind`, `elec_fresh`, `cycles_fresh`. When `blind`,
`add_sample` charges the interval to `unknown_s` and returns immediately —
no thermal energy, no mode seconds, no Carnot term, no counter endpoints.
Deliberately all-or-nothing: partially trusting a seeded snapshot is how a
restart becomes a fault signature. The two counter endpoints are gated
*separately* because a stale endpoint corrupts Δ directly even on an
otherwise-live sample.

### 3. Unobserved time is accounted, not discarded
This is the substantive design change, and it is broader than the reported
defect. v0.3.2 zeroed over-long intervals (the gap cap) — correct in that it
prevented phantom integration, but it left the *consequence* invisible. The
counters do not pause: after any blind window or outage, Δ23009 covers
energy that `thermal_kwh` structurally cannot. Skipping the interval does
not repair the mismatch, it merely hides it.

So blind intervals and over-long gaps now accumulate into
`_DayAccumulator.unknown_s`, and day close rejects `stale_restore` when it
exceeds `HEALTH_MAX_UNKNOWN_S`. **This also fixes plain HA outages**, which
v0.3.2 tolerated silently — a genuine second defect found while fixing the
first.

Bound derivation (not a round number picked for looks): qualifying days
carry ≥ `HEALTH_MIN_ELEC_KWH` = 5 kWh. Holding the induced PF error below
~5 % requires the unobserved window to cover < 0.25 kWh, which at this
unit's ~1.5 kW typical compressor draw is ~10 minutes. Hence 600 s.

Negative time steps (clock adjustments) are dropped and explicitly **not**
charged to `unknown_s` — they carry no reliable duration, so inventing one
would be worse than ignoring them.

### 4. Two-tier readiness
A pure freshness gate deadlocks: CAN broadcasts on change, so an idle
machine's modulation sits at 0 and never re-broadcasts, and the integration
is read-only and cannot request a re-send. A pure timer is also wrong — it
resumes fabricating data the moment it expires. Hence:

- **`HEALTH_FRESH_REQUIRED`** (modulation, thermal power) — no timeout,
  ever. These drive mode classification and the thermal integral, so a
  stale value is corrupting rather than merely unhelpful. They self-heal
  within minutes *whenever the machine actually runs* — which is exactly
  when they matter. If the machine never runs, the day correctly fails to
  qualify on SH time anyway.
- **`HEALTH_SETTLE_REQUIRED`** (statuses, heat-gen temp) — freshness **or**
  `HEALTH_SETTLE_S` = 3600 s. These are gates, not integrands; after the
  window an un-rebroadcast value is far more likely genuinely unchanged than
  stale, and a wrong gate is lossy (day rejected) rather than corrupting.

The owner's proposed flat one-hour suppression would also have worked and is
the same order of magnitude; the tiered version additionally resumes early
(typically ~6 minutes, per this installation's observed 2026-07-17 restart)
and never resumes into stale integrands, which a bare timer cannot promise.

### 5. Observability
`last_day_unobserved_h` on Health Status; `sampling_blind` and
`open_day_unknown_s` in diagnostics; one debug log on each blind→live
transition (edge-triggered, not per tick). Consistent with the project's
standing rule that rejected days state *why*.

## Reviewed, acceptable

- **Days can now be rejected that v0.3.2 accepted.** Intended. Those days
  were contaminated; rejecting them is the fix, not a side effect. Expected
  cost is roughly one qualifying day per restart that lands in a heating
  window — negligible against a 90-day baseline, and visible via the
  confidence sensor's yield component if restarts ever become frequent.
- **No config option for the settle window or the unobserved bound.**
  Consistent with the project's stance against user-tunable statistical
  parameters; both are documented `HEALTH_*` constants with derivations.
- **Program (DpId 502) still not persisted.** Correct as-is: absent, the
  gate returns None and never blocks, and blind intervals are now discarded
  wholesale regardless. Adding it to `PERSISTENT_DPIDS` would create exactly
  the stale-gate problem this release removes.
- **`unknown_s` is not carried across a day boundary.** A new day starts
  clean; blindness at the start of a day is harmless because the counter
  endpoints and the thermal integral both begin at the first observed
  sample, so they remain mutually consistent.
- **The derived power sensors still use seeded values.** Unchanged and
  correct — they are instantaneous consumers, and known gap #9 remains open
  for them per the standing owner decision. Only the integrating consumer
  was fixed.

## Failure-mode analysis (delta vs v0.3.2)

| Failure | v0.3.2 | v0.3.3 |
|---|---|---|
| Restart while compressor running | Phantom heat integrated for up to 1 h; η inflated; **silently qualifies** | Intervals blind → nothing integrated, charged to `unknown_s`; day rejected `stale_restore` |
| Restart while compressor idle | Real heat missed while Δ23009 counts it; η deflated → **false alarm possible** | Same protection |
| Plain HA outage, no stale data | Gap capped, but counter/integral mismatch left silent | Charged to `unknown_s`; rejected past 10 min |
| Stale counter endpoint at day close | Δelec understated → PF overstated | Endpoint not captured until the counter goes live |
| Idle machine, modulation never re-broadcasts | — | FRESH tier keeps sampling blind; day fails SH-time anyway. No deadlock, no fabrication |
| Status seeded but never re-broadcast | — | SETTLE tier releases after 1 h; gate uses the seeded value, which by then is almost certainly current |
| Clock step backwards | Interval dropped | Dropped, and not miscounted as unobserved |
| Brief blindness (< 10 min) | — | Tolerated; day still qualifies |

**Resource impact:** one float per accumulator, two booleans per sample, one
set membership test per gated dp_id per tick. No new timers, no new storage
keys, no change to write frequency.

## Test evidence

`tests/test_health.py` — cold-start group: blind samples fabricate no
energy/mode-seconds/Carnot; stale counter endpoints refused then captured
once live; long gaps charged as unobserved; brief blindness tolerated;
start-of-day blindness harmless; clock steps not miscounted. Centrepiece is
an **end-to-end reproduction of the defect**: the same simulated restart day
is run twice, with and without the fix, asserting that the pre-fix path
integrates > 3 kWh of phantom heat and **qualifies silently**, while the
fixed path rejects with `stale_restore`. A fix whose test cannot demonstrate
the original failure is not a verified fix.

`tests/test_protocol.py` — tracker-level gate test driving a **real
coordinator** (not a fake) with a populated `_restored_dpids`: no phantom
thermal energy or SH seconds while seeded, no counter endpoints captured,
blind time accounted, sampling resumes once live frames arrive, and both
readiness tiers verified independently (settle tier blocks inside the window
and releases after it). The complete v0.3.2 regression set — protocol
framing, watchdogs, integrators, persistence, diagnostics, rates, health
statistics — passes unchanged, confirming the fix is additive.

One test defect was found and corrected during the cycle: an assertion
compared an absolute value where the tracker under test had restored a
persisted model, and needed to compare a delta. Code was correct; the test
was wrong.

## Field expectations

No visible change until the heating season. When restarts occur, the
affected day is dropped rather than silently contaminating the baseline;
`last_day_unobserved_h` and `last_day_reject_reasons` on the Health Status
entity show exactly what happened. On this installation's observed restart
behaviour (~6 minutes to live modulation/status/counter frames), a restart
outside an active heating window typically costs nothing at all.
