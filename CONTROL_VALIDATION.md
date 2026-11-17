# SRS controller validation — November 16, 2026

The saved 105-run comparison below uses the earlier three-second averaging window. The current local configuration is one second and is preserved in the controller. The unit tests also exercise the current setting. To reproduce the saved comparison, use `--average-seconds 3`; omit that option to evaluate the current configured window.

All results below are hardware-free simulations, not experimental validation. No heaters were actuated. The configured 150 C safety limit was preserved. The user selected 30-second minimum ON/OFF dwell, replacing the conflicting earlier 60-second requirement.

## Evidence and identification

The local 50 C run (`logs/2026-11-16_20-05-16_559797/data.csv`) contains 431 rows over 2197 seconds, with a 57.00285 C maximum and a later minimum near 44.6 C. At 50.504 C on the first rise, demand was 2.587 heaters and all three software states were ON. The user subsequently confirmed heater 0 was unplugged for this run. Its physical heat contribution is therefore taken as ZERO, even when its software state is ON. The other short November 16 logs provide ambient/startup context. No November 14 logs were found under the SRS project.

The model is `dq/dt=(u_delayed-q)/lag`, `dT/dt=k*q-loss*(T-21.8)`. Input u is the sum of logged states of heaters 1 and 2, held between samples; it is never `pi_output`. There is no electrical contact/current feedback, so states 1 and 2 are assumed to correspond to delivered heat. Synthetic healthy cases assume equal heat from each connected element; heater 0 was not independently characterized by this run. A disconnected element cannot be discovered by this software.

An offline free-run least-squares fit used the whole 50 C run, 2 s integration, ZOH state reconstruction, and fitted initial stored heat. Search bounds were k=0.008–0.15 C/s/heater, loss=0.0001–0.006 /s, lag=5–240 s, delay=0–100 s and initial heat=0–2 heater equivalents (SciPy differential evolution, seed 42, population multiplier 10, max 150 iterations). This analysis used an already installed scientific package; the controller, tests and replay harness add no dependency.

Best provisional fit: k=0.02811 C/s/heater; loss=0.0008753 /s (cooling time constant 19.0 min); lag=5.0 s; delay=0.6 s; RMSE=1.58 C; maximum free-run error=3.18 C. Lag reached its lower search bound and stored heat its upper bound. **This is not a reliable identification of inertia.** Correcting the disconnected heater substantially changes the inferred dynamics; fitting as if all three worked would be misleading.

Observed 30-second temperature differences span approximately +3.67 to -2.11 C/min. The linear fit implies about 0.88 connected-heater equivalents to hold 50 C and cooling near 1.48 C/min at 50 C. Those estimates are local and uncertain, not feedforward calibration. Linear extrapolation gives about 1.50 heaters at 70 C, 2.44 at 100 C and 3.99 at 150 C; these high-temperature values are not established by the data. Fixed heat-loss feedforward was deliberately omitted.

## Selected control method

Industrial oven controllers separate large-step approach/cutback behavior from steady regulation. Eurotherm describes cutback as a way to control start-up overshoot while allowing responsive regulation ([manufacturer handbook](https://www.eurotherm.com/?wpdmdl=27913)). Measurement-based rate action avoids the derivative kick from setpoint changes ([Control Station / Control Guru](https://controlguru.com/pid-control-and-derivative-on-measurement/)). Integral limiting alone does not address every windup problem; saturation-aware integration is established practice ([Control Guru anti-windup discussion](https://controlguru.com/integral-reset-windup-jacketing-logic-and-the-velocity-pi-form/)).

The implementation uses one equation: predicted temperature = average + 60 s * filtered rate; demand = clamp(KP * predicted error + integral, 0, N). The measurement slope has a 20 s exponential filter. There is no separate derivative branch or fixed feedforward. KP=0.25 heater/C and KI=0.001 heater/(C*s) act on predicted error. This is a strategy change, not just a gain change.

Large measured errors outside max(10 C, N/KP) reset/keep integral at zero. On a normal warm-up the same equation crosses into regulation with integral initially zero, rather than a stored full-power bias. No mode changes force a discrete output jump on that normal approach. Within regulation, positive integration stops at upper saturation; negative predicted error can always unload the nonnegative integral, even at lower saturation. A later large disturbance or setpoint step can deliberately clear the integral again. This is not a promise of bumpless output for arbitrary target changes or large disturbances.

The old controller accumulated integral during a wide approach band and retained it at the setpoint, while ignoring temperature rise. Its scheduler discarded fractions near integers and used requested-slot fractions rather than an elapsed delivered-energy balance. The replacement tracks heater-seconds, including dwell/late-slot delivery, and ignores old duty debt at zero/full saturation. It picks an integer stage for each slot from demand PLUS energy error. Per-relay dwell remains an independent hard normal-control constraint. Assignments use accumulated runtime; steady integer loads rotate occasionally.

## Simulation method and comparison

Each case runs for three hours with 0.1 s exact two-state plant updates, 5 s control updates, ZOH delayed actual relay states, and individual relay dwell. Baseline is the previous KP=0.10, KI=0.0005 PI with its original 60 s scheduler. A third arm uses the old PI with the NEW 30 s scheduler, to distinguish control-law improvement from faster scheduling. The revised controller uses the same three-second average as production; baseline arms retain instantaneous measurements. There is no hardware driver import. Safety is checked each simulated 0.1 second using four sensors with offsets -0.2, -0.1, +0.1, +0.2 C. After a fault the simulated outputs remain latched OFF, but the model continues passive coasting for the rest of the run, so residual-heat peaks are included.

The nominal model uses the rounded local k/loss estimates, but deliberately assumes 70 s heat lag and 15 s delay. Stress models span 5–180 s lag, 1–45 s delay, k=0.022–0.045, loss=0.0007–0.0012, and an additional temperature-dependent heat-loss case. These are sensitivity assumptions, not measured plant bounds. The sweep compares 12 choices of KP, KI and lookahead (0/60/120 s) on 50/70 C scenarios. The chosen 0.25/0.001/60 s had the lowest defined score (settling + overshoot penalty + tail bias) in that original pre-averaging sweep; this is not a global optimum. The three-second averaging change did not retune the gains; the full comparison below was rerun with averaging and 10 Hz sampling. 100/150 C cases and further disturbances are then evaluated separately.

Nominal model, three connected heaters, 21.8 C cold start:

| Target | Controller | Overshoot (C) | Settling to +/-2 C (min) | Final 30 min mean error (C) | Max final error (C) | Max relay transitions/hour |
|---:|---|---:|---:|---:|---:|---:|
| 50 | Old PI + old scheduler | 9.13 | 38.94 | 0.03 | 0.57 | 7.33 |
| 50 | Old PI + new scheduler | 7.86 | 29.25 | 0.00 | 0.24 | 13.67 |
| 50 | Predictive PI + new scheduler | 0.45 | 9.61 | -0.01 | 0.30 | 11.00 |
| 70 | Old PI + old scheduler | 6.68 | 36.02 | -0.02 | 0.57 | 15.67 |
| 70 | Old PI + new scheduler | 5.41 | 25.59 | 0.00 | 0.26 | 32.00 |
| 70 | Predictive PI + new scheduler | 0.31 | 16.20 | -0.00 | 0.20 | 28.67 |
| 100 | Old PI + old scheduler | 3.17 | 43.17 | -0.03 | 0.48 | 14.33 |
| 100 | Old PI + new scheduler | 2.12 | 38.75 | 0.01 | 0.17 | 30.00 |
| 100 | Predictive PI + new scheduler | 0.31 | 32.18 | 0.00 | 0.21 | 29.67 |
| 150 | Old PI + old scheduler | 0.00 | -- | -31.88 | 31.90 | 0.33 |
| 150 | Old PI + new scheduler | 0.00 | -- | -31.88 | 31.90 | 0.33 |
| 150 | Predictive PI + new scheduler | 0.00 | -- | -31.88 | 31.90 | 0.33 |

Settling is the last excursion outside +/-2 C, with at least 10 minutes remaining inside the band; no settling result is reported for a faulted/unsettled case. Tail errors use the final 30 minutes. Relay rate is the busiest individual relay, averaged until fault or simulation end, including startup. More relay transitions are required than with the old 60-second schedule in several cases: the 30-second choice trades mechanical lifetime for finer power staging. Every normal transition interval in the completed suite is at least 30 s. Safety shutdowns are excluded from that dwell metric.

## Limits exposed by the simulations

- Healthy, reachable static 50/70/100 C cases in this selected envelope: revised overshoot at most about 1.07 C; final 30-minute maximum absolute error at most about 0.50 C. Depending on capacity and target, settling still takes minutes to tens of minutes. These are synthetic results, not measured accuracy.
- Nominal model with 1/2/4 connected heaters at 50 C settles in about 33.7/12.3/8.0 min. All 15 nonempty channel subsets and integer demands are additionally tested directly.
- With heater 0 unplugged but still configured, the nominal 50 C simulation cycles up to about 1.92 C away from target and never meets the strict all-future +/-2 C settling definition during the run. Software cannot guarantee uniform delivered power while rotating through a physically dead element. If an element is intentionally disconnected, remove its channel from HEATER_CHANNELS rather than pretending it supplies heat.
- A 5 C door-loss event at 90 min plus a 25% heat-loss increase at 120 min recovers; the long-lag 70 C stress case reaches about 2.60 C overshoot after disturbance and finally re-enters +/-2 C around 101.2 min from startup (about 11.2 min after the door event). No universal transient +/-2 C guarantee is claimed.
- Three heaters in the nominal linear model can reach only about 118.1 C at full power; the weak/high-loss case cannot reach 100 C. This is an assumed capacity limit, not a measured upper temperature of the box. No controller can correct insufficient heat capacity.
- A 150 C average target coincides with the existing individual-sensor cutoff. Where a model has enough capacity, the hottest sensor reaches the 150 C limit and latches OFF before useful regulation can be established. Four nominal heaters also trigger this protection. In the fast three-heater case, residual heat continues to raise the average to about 150.04 C after the trip, despite latched OFF outputs. The cutoff was NOT raised and the target was NOT silently reduced. A usable target below the hard cutoff needs actual hotspot/lag margin.
- The original experiment is too compromised and narrow to prove operation at 70/100/150 C or +/-2 C on hardware. Fresh connected-heater heating/coasting/cooling logs are needed before treating these estimates as calibration. Independent hardware overtemperature protection remains necessary.

## Reproduction and tests

```bat
py -3.11 -m unittest -v
py -3.11 validate_control.py --average-seconds 3 --output validation_results.json
py -3.11 validate_control.py --sweep
```

22 deterministic test methods pass on Python 3.12.2. They cover the 1–4 channel subsets, integer/fractional/near-integer requests, elapsed-time energy balance under jitter, runtime fairness, 30 s dwell including slow writes, rapid targets, predictive cutback, warm-up integral exclusion, anti-windup, STOP/Ctrl+C, fault latching, sensor errors, callback races, partial attachment cleanup, explicit simulation isolation, three-second window averaging with jitter and partial startup, raw faults bypassing smoothing, minimum device-interval configuration, post-trip passive coasting, and CSV/JSON structure. Hardware control is the default; simulation requires `--simulate`. All hardware-mode tests mock device attachment and outputs. The comparison harness completes 105 runs. Full results and model parameters are in validation_results.json. No hardware operation was proposed or performed by these tests.
