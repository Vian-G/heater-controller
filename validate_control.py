"""Offline comparison; stdlib only, no Phidgets imports or hardware access.

python validate_control.py --output validation_results.json
The models are deliberately provisional. Relay states, not demand, heat the plant.
"""
import argparse
import csv
import json
import math
from collections import deque
from datetime import datetime
from pathlib import Path

import heater as h

# k (C/s/heater), heat loss (1/s), thermal lag (s), transport delay (s).
MODELS = {
    "corrected_fit": (0.028106, 0.0008753, 5.0, 1),
    "nominal_lag": (0.0281, 0.000875, 70.0, 15),
    "long_lag": (0.0281, 0.000875, 180.0, 45),
    "fast_low_loss": (0.045, 0.0007, 40.0, 10),
    "weak_high_loss": (0.022, 0.0012, 120.0, 30),
    "nonlinear_loss": (0.0281, 0.000875, 90.0, 15),
}


class LegacyPI:
    """Previous controller's exact PI math for comparison, not production."""
    def __init__(self, count):
        self.count, self.integral, self.last_time = count, 0.0, None

    def update(self, target, average, now):
        dt = 0 if self.last_time is None else now - self.last_time
        self.last_time = now
        error = target - average
        p = 0.10 * error
        raw = p + self.integral
        if not ((raw >= self.count and error > 0) or (raw <= 0 and error < 0)):
            self.integral = h.clamp(self.integral + .0005 * error * dt, 0, self.count)
        return h.clamp(p + self.integral, 0, self.count)


class LegacyRelays(h.Relays):
    """Original 60 s slot accumulator, including integer snapping and rotation."""
    def __init__(self, channels, now, **kwargs):
        super().__init__(channels, now, **kwargs)
        self.fraction = 0.0

    def schedule(self, demand, now):
        self.account(now)
        if now < self.next_slot:
            return
        self.next_slot = now + 60
        demand = h.clamp(demand, 0, len(self.channels))
        nearest = round(demand)
        if abs(demand - nearest) <= .01:
            desired, self.fraction = nearest, 0.0
        else:
            desired = math.floor(demand)
            self.fraction += demand - desired
            if self.fraction >= 1 - 1e-9:
                desired += 1
                self.fraction = max(0, self.fraction - 1)
        eligible = [c for c in self.channels if self.last_transition[c] is None
                    or now - self.last_transition[c] >= 60]
        on = sorted((c for c in eligible if self.on[c]),
                    key=lambda c: (-self.on_seconds[c], c))
        off = sorted((c for c in eligible if not self.on[c]),
                     key=lambda c: (self.on_seconds[c], c))
        delta = desired - sum(self.on.values())
        if delta < 0:
            for c in on[:-delta]:
                self.change(c, False, now)
        elif delta > 0:
            for c in off[:delta]:
                self.change(c, True, now)
        elif (on and off and now - self.last_rotation >= 600
              and self.on_seconds[on[0]] - self.on_seconds[off[0]] >= 600):
            self.change(on[0], False, now)
            self.change(off[0], True, now)
            self.last_rotation = now
        self.next_slot = max(self.next_slot, max(
            (t + 60 for t in self.last_transition.values() if t is not None), default=now))


def simulate(target=50, count=3, model="nominal_lag", strategy="predictive",
             duration=10800, disconnected=(), disturbance=False, noise=0.0,
             initial=21.8, tuning=None, capture=False):
    """10 Hz plant/raw readings, 5 s control, independent relay dwell and safety."""
    step = h.LOOP_INTERVAL_SECONDS
    update_ticks = round(5 / step)
    clock = [0.0]
    channels = tuple(range(count))
    control = (LegacyPI(count) if strategy.startswith("legacy") else
               h.Controller(count, **(tuning or {})))
    relay_type = LegacyRelays if strategy == "legacy" else h.Relays
    relays = relay_type(channels, 0, clock=lambda: clock[0])
    k, loss, lag, delay = MODELS[model]
    powers = deque([0.0] * max(1, round(delay / step)))
    history = deque()
    temp, heat, demand = initial, 0.0, 0.0
    temperatures, trace, normal_intervals = [], [], []
    fault, fault_time = "", None
    for tick in range(round(duration / step)):
        now = tick * step
        clock[0] = float(now)
        if disturbance and now == 5400:
            temp -= 5.0  # Door-open heat loss.
        measured = temp + noise * math.sin(now * .37)
        if not fault:
            try:
                average = h.validate_temperatures([measured + v for v in (-.2, -.1, .1, .2)])
            except ValueError as error:
                relays.shutdown(str(error), now)
                fault, fault_time = str(error), now
            if not fault:
                history.append((now, (measured,) * 4))
                if tick % update_ticks == 0:
                    if strategy == "predictive":
                        average = sum(h.average_temperatures(history, now)) / 4
                    else:
                        history.clear()  # Baseline used instantaneous measurements.
                    demand = control.update(target, average, now)
                previous = dict(relays.last_transition)
                relays.schedule(demand, now)
                for c in channels:
                    if relays.last_transition[c] != previous[c] and previous[c] is not None:
                        normal_intervals.append(now - previous[c])
        # After a latched fault, keep simulating passive coasting: stored heat
        # can still raise temperature even though every output is OFF.
        # A disconnected element supplies NO heat even when software says ON.
        powers.append(sum(relays.on[c] for c in channels if c not in disconnected))
        actual = powers.popleft()
        effective_loss = loss * (1.25 if disturbance and now >= 7200 else 1)
        if model == "nonlinear_loss":
            effective_loss *= 1 + .004 * max(0, temp-50)
        temp, heat = h.thermal_step(temp, heat, actual, step, k, effective_loss, lag)
        temperatures.append(temp)
        if capture and tick % update_ticks == 0:
            trace.append((now, temp, demand, sum(relays.on.values()), control.integral))
    elapsed = max(step, len(temperatures) * step)
    tail = temperatures[-min(round(1800 / step), len(temperatures)):]
    last_bad = max((i for i, v in enumerate(temperatures) if abs(v-target) > 2), default=-1)
    settled = ((last_bad + 1) * step / 60
               if not fault and elapsed - (last_bad + 1) * step >= 600 else None)
    result = dict(strategy=strategy, target_C=target, count=count, model=model,
                  disconnected=list(disconnected), disturbance=disturbance, initial_C=initial, noise_C=noise,
                  elapsed_s=elapsed, fault=fault, fault_time_s=fault_time,
                  final_heaters_on_count=sum(relays.on.values()), peak_C=max(temperatures, default=initial),
                  overshoot_C=max(0, max(temperatures, default=initial)-target),
                  settling_min=settled, mean_tail_error_C=sum(tail)/len(tail)-target,
                  max_tail_abs_error_C=max(abs(v-target) for v in tail),
                  tail_peak_to_peak_C=max(tail)-min(tail),
                  max_transitions_per_relay_per_hour=max(relays.transitions.values())*3600/max(1, fault_time or elapsed),
                  minimum_normal_dwell_s=min(normal_intervals, default=None),
                  theoretical_full_power_equilibrium_C=21.8+k*(count-len(disconnected))/loss,
                  runtime_spread_s=max(relays.on_seconds.values())-min(relays.on_seconds.values()))
    if fault:
        for field in ("mean_tail_error_C", "max_tail_abs_error_C", "tail_peak_to_peak_C"):
            result[field] = None  # Never describe an interrupted warm-up as steady state.
    if capture:
        result["trace"] = trace
    return result


def inspect_logs(root=Path("logs")):
    runs = []
    for file in sorted(root.glob("*/data.csv")):
        with file.open(newline="", encoding="utf-8") as log:
            rows = list(csv.DictReader(log))
        if not rows:
            continue
        valid = [r for r in rows if r.get("avg_C") and math.isfinite(float(r["avg_C"]))]
        if not valid:
            continue
        values = [float(r["avg_C"]) for r in valid]
        stamps = [datetime.fromisoformat(r["timestamp"]) for r in valid]
        # Physical heater-0 omission applies ONLY to the user-identified last run.
        missing = (0,) if file.parent.name == "2026-11-16_20-05-16_559797" else ()
        runs.append(dict(run=file.parent.name, samples=len(rows),
                         duration_s=(stamps[-1]-stamps[0]).total_seconds(),
                         min_C=min(values), max_C=max(values),
                         physical_disconnected_channels=list(missing)))
    return runs


def suite():
    cases = [dict(target=t) for t in (50, 70, 100, 150)]
    cases += [dict(target=t, model=m) for m in MODELS if m != "nominal_lag"
              for t in (50, 70, 100, 150)]
    cases += [dict(target=50, count=n) for n in (1, 2, 4)]
    cases += [dict(target=t, disconnected=(0,)) for t in (50, 70, 100)]
    cases += [dict(target=50, disturbance=True, noise=.08),
              dict(target=70, disturbance=True, model="long_lag"),
              dict(target=150, count=4), dict(target=150, count=4, model="fast_low_loss"),
              dict(target=50, initial=60)]
    return [simulate(**case, strategy=strategy) for case in cases
            for strategy in ("legacy", "legacy_new_scheduler", "predictive")]


REPORT_METADATA = {'identification': {'source_run': '2026-11-16_20-05-16_559797',
                    'excluded_physical_heaters': [0],
                    'ambient_C': 21.8,
                    'method': 'Free-run two-state model least-squares; ZOH relay states, 2 s '
                              'integration, initial stored heat fitted. Offline scipy '
                              'differential_evolution, seed 42.',
                    'fit': {'heat_rate_C_per_s_per_heater': 0.02810615795103466,
                            'loss_per_s': 0.0008752639820854175,
                            'lag_s': 5.000000936865933,
                            'delay_s': 0.5541075323400193,
                            'initial_heat': 1.9999993583656146,
                            'rmse_C': 1.581876629049302,
                            'max_error_C': 3.177654838056725},
                    'warning': 'Lag hit the 5 s lower bound and initial heat hit the 2-heater '
                               'upper bound: inertia is not reliably identified. High-temperature '
                               'predictions are extrapolations.'},
 'metric_definitions': {'settling_min': 'Last exit from +/-2 C plus one simulation step, with >=600 s '
                                        'remaining inside band, measured from start; null if not '
                                        'settled or faulted.',
                        'mean_tail_error_C': 'Mean signed error during final 1800 s; null after '
                                             'safety fault.',
                        'max_tail_abs_error_C': 'Maximum absolute error during final 1800 s; null '
                                                'after safety fault.',
                        'max_transitions_per_relay_per_hour': 'Largest per-relay transition count '
                                                              'divided by hours until fault or '
                                                              'simulation end, including startup '
                                                              'and safety transition.',
                        'minimum_normal_dwell_s': 'Minimum between normal relay changes; excludes '
                                                  'immediate safety shutdown.',
                        'initial_C': '21.8 except explicit 60 C hot restart; no physically stored '
                                     'energy at start of synthetic runs.',
                        'peak_C': 'Maximum model average over the entire run, including passive '
                                  'coasting AFTER a latched OFF fault; may exceed the software '
                                  'cutoff.'}}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--average-seconds", type=float, default=h.TEMPERATURE_AVERAGE_SECONDS,
                        help="averaging window for this offline comparison only")
    args = parser.parse_args()
    if not math.isfinite(args.average_seconds) or args.average_seconds <= 0:
        parser.error("--average-seconds must be finite and positive")
    h.TEMPERATURE_AVERAGE_SECONDS = args.average_seconds
    if args.sweep:
        ranked = []
        for kp in (.25, .4):
            for ki in (.0005, .001):
                for lookahead in (0, 60, 120):
                    tuning = dict(kp=kp, ki=ki, lookahead=lookahead)
                    results = [simulate(target=t, model=m, tuning=tuning)
                               for m in ("corrected_fit", "nominal_lag", "long_lag", "fast_low_loss")
                               for t in (50, 70)]
                    score = sum(30*max(0, r["overshoot_C"]-2)+(r["settling_min"] or 300)
                                +20*abs(r["mean_tail_error_C"]) for r in results)
                    ranked.append(dict(score=score, tuning=tuning))
        print(json.dumps(sorted(ranked, key=lambda r:r["score"]), indent=2))
        return
    results = suite()
    data = dict(models=MODELS, duration_s=10800, control_interval_s=5,
                sensor_poll_interval_s=h.LOOP_INTERVAL_SECONDS,
                temperature_average_s=h.TEMPERATURE_AVERAGE_SECONDS,
                minimum_dwell_s=h.MIN_DWELL_SECONDS, logs=inspect_logs(), results=results,
                **REPORT_METADATA)
    if args.output:
        args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("model target n strategy peak overshoot settling_min tail_error tail_maxabs switches/h fault")
    for r in results:
        if r["model"] == "nominal_lag" and not r["disconnected"] and not r["disturbance"] and r["count"] == 3 and r["initial_C"] == 21.8:
            print(r["model"],r["target_C"],r["count"],r["strategy"],
                  *(round(r[k],2) if r[k] is not None else "--" for k in (
                      "peak_C","overshoot_C","settling_min","mean_tail_error_C",
                      "max_tail_abs_error_C","max_transitions_per_relay_per_hour")), r["fault"])
    print("Completed", len(results), "hardware-free runs.")


if __name__ == "__main__":
    main()
