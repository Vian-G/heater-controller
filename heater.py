"""Predictive PI controller. Hardware by default; --simulate disables actuation.

Independent hardware overtemperature protection remains necessary.
"""

import argparse
import csv
import json
import math
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from time import monotonic

HEATER_SERIAL = 343887
SENSOR_SERIAL = 341989
HEATER_CHANNELS = (0, 1, 2)  # Any nonempty subset of (0, 1, 2, 3).
SENSOR_CHANNELS = (0, 1, 2, 3)
KP = 0.25  # Heater equivalents / C of predicted temperature error.
KI = 0.001  # Heater equivalents / (C * second) of predicted error.
LOOKAHEAD_SECONDS = 60.0  # Early cutback; provisional, NOT an identified delay.
RATE_FILTER_SECONDS = 20.0  # Filter measurement slope; no derivative setpoint kick.
INTEGRAL_BAND_C = 10.0  # Expanded to N/KP if needed to permit offset removal.
# No fixed heat-loss feedforward: the available test had an unplugged heater.
# These settings are simulation-tested, not a guarantee of +/-2 C on hardware.
MAX_SAFETY_TEMPERATURE_C = 150.0  # Review for actual box/materials before use.
MAX_SENSOR_SPREAD_C = 30.0
LOG_INTERVAL_SECONDS = 5.0  # Also the PI update interval.
SCHEDULE_INTERVAL_SECONDS = 30.0
MIN_DWELL_SECONDS = 30.0
ROTATION_SECONDS = 600.0  # Rotate steady integer loads at most once per 10 min.
LOOP_INTERVAL_SECONDS = 0.1  # 10 Hz polling and raw safety checks.
TEMPERATURE_AVERAGE_SECONDS = 1.0  # Trailing window; partial at startup, no wait.
ATTACH_TIMEOUT_MS = 5000
LOG_DIR = Path(__file__).parent / "logs"


def clamp(value, low, high):
    return max(low, min(value, high))


def validate_temperatures(values):
    """Missing, nonnumeric and nonfinite readings are faults, never zero-filled."""
    if values is None or len(values) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) for v in values):
        raise ValueError("Invalid or missing thermocouple reading")
    if max(values) > MAX_SAFETY_TEMPERATURE_C:
        raise ValueError("Maximum safety temperature exceeded")
    if max(values) - min(values) > MAX_SENSOR_SPREAD_C:
        raise ValueError("Sensor spread exceeds 30 C")
    return sum(values) / 4


def average_temperatures(history, now):
    """Average timestamped, already validated four-channel samples in (now-3, now].

    Prune by elapsed time rather than sample count, so polling jitter does not
    change the window. Callers validate EVERY raw sample before buffering it.
    """
    while history and history[0][0] <= now - TEMPERATURE_AVERAGE_SECONDS:
        history.popleft()
    if not history:
        raise ValueError("No temperature samples in averaging window")
    return [math.fsum(values[ch] for _, values in history) / len(history)
            for ch in SENSOR_CHANNELS]


class Controller:
    """PI on a short temperature prediction, with separated warm-up integral.

    The same output equation is used throughout: no ON/OFF warm-up handoff.
    Outside the regulation band the integral is zero; entering the band enables
    integration from zero, without preloading a saturated warm-up demand.
    """
    def __init__(self, count, kp=KP, ki=KI, lookahead=LOOKAHEAD_SECONDS):
        self.count, self.kp, self.ki, self.lookahead = count, kp, ki, lookahead
        self.integral = self.rate = 0.0
        self.previous = None
        self.predicted = 0.0
        self.mode = "WARMUP"

    def update(self, target, average, now):
        if not all(math.isfinite(v) for v in (target, average, now)):
            raise ValueError("Nonfinite controller input")
        dt = 0.0
        if self.previous is not None:
            dt = now - self.previous[0]
            if dt <= 0:
                raise ValueError("Controller time must increase")
            measured_rate = (average - self.previous[1]) / dt
            self.rate += -math.expm1(-dt / RATE_FILTER_SECONDS) * (measured_rate - self.rate)
        self.previous = now, average
        self.predicted = average + self.lookahead * self.rate
        error = target - average
        predicted_error = target - self.predicted
        proportional = self.kp * predicted_error
        band = max(INTEGRAL_BAND_C, self.count / self.kp)
        self.mode = "WARMUP" if error > band else "COAST" if error < -band else "REGULATE"
        if abs(error) > band:
            self.integral = 0.0  # Never integrate a large warm-up or step error.
        elif predicted_error < 0 or proportional + self.integral < self.count:
            # Negative predicted error can ALWAYS unload stored heat demand,
            # even at zero output. A nonnegative integral cannot wind down below 0.
            self.integral = clamp(self.integral + self.ki * predicted_error * dt,
                                  0.0, float(self.count))
        return clamp(proportional + self.integral, 0.0, float(self.count))


def thermal_step(temperature, heat, power, seconds, heat_rate=0.0281,
                 loss=0.000875, lag=70.0, ambient=21.8):
    """Exact ZOH two-state illustrative plant, NOT a hardware safety model.

    heat_rate: C/s/heater; loss: 1/s; lag: seconds. heat is stored heater
    output in heater equivalents. Offline validation also adds transport delay.
    """
    a, b = math.exp(-seconds / lag), math.exp(-loss * seconds)
    cross = ((a - b) / (loss - 1 / lag) if abs(loss - 1 / lag) > 1e-12
             else seconds * b)
    temperature = (ambient + (temperature - ambient) * b + heat_rate * (
        power * -math.expm1(-loss * seconds) / loss + (heat - power) * cross))
    return temperature, power + (heat - power) * a


class Relays:
    """Heater-second error diffusion, per-channel dwell, runtime balancing.

    outputs maps enabled channel -> Phidget (or mock) with setState(bool).
    All writes/stops share a lock so a latched stop cannot be re-enabled.
    State is last successfully commanded state, NOT contact feedback.
    """
    def __init__(self, channels, now, outputs=None, clock=None):
        if not channels or len(set(channels)) != len(channels) or any(
                type(ch) is not int or ch not in SENSOR_CHANNELS for ch in channels):
            raise ValueError("HEATER_CHANNELS must be a unique subset of 0,1,2,3")
        self.channels = tuple(channels)
        self.clock = monotonic if clock is None else clock
        self.outputs = {} if outputs is None else outputs
        self.on = {ch: False for ch in channels}
        self.on_seconds = {ch: 0.0 for ch in channels}
        self.transitions = {ch: 0 for ch in channels}
        # No transition history at startup: the first valid demand may heat now.
        self.last_transition = {ch: None for ch in channels}
        self.accounted_at = now
        self.next_slot = now
        self.last_rotation = now
        self.energy_error = self.last_demand = 0.0
        self.state, self.reason = "RUNNING", ""
        self.shutting_down = False
        self.lock = threading.RLock()
        self.stopped = threading.Event()

    def account(self, now):
        # A callback may have accounted a slightly later timestamp already.
        now = max(now, self.accounted_at)
        dt = now - self.accounted_at
        if not self.stopped.is_set():
            self.energy_error += (self.last_demand - sum(self.on.values())) * dt
        for ch in self.channels:
            if self.on[ch]:
                self.on_seconds[ch] += dt
        self.accounted_at = now

    def change(self, ch, state, now):
        if state and self.stopped.is_set():
            return
        if ch in self.outputs:
            self.outputs[ch].setState(state)
        # An error callback can latch a stop during an ON write. Reassert OFF.
        if state and self.stopped.is_set():
            if ch in self.outputs:
                self.outputs[ch].setState(False)
            state = False
        # Dwell starts after the write returns, not at an earlier sensor sample.
        now = max(now, self.clock())
        self.account(now)
        if self.on[ch] != state:
            self.on[ch] = state
            self.transitions[ch] += 1
            self.last_transition[ch] = now

    def shutdown(self, reason, now=None, fault=True):
        """Latch first reason, bypass dwell, attempt EVERY output despite errors."""
        with self.lock:
            now = monotonic() if now is None else now
            if not self.stopped.is_set():
                self.state = "FAULT" if fault else "STOPPED"
                self.reason = reason
                self.stopped.set()
            # Avoid recursive OFF attempts if the API reports another error
            # synchronously from an OFF command. The outer shutdown tries all.
            if self.shutting_down:
                return
            self.shutting_down = True
            self.account(now)
            errors = []
            try:
                for ch in self.channels:
                    try:
                        self.change(ch, False, now)  # Send OFF even if tracked OFF.
                    except Exception as error:
                        errors.append(f"OFF failed for heater {ch}: {error}")
            finally:
                self.shutting_down = False
            if errors:
                self.state = "FAULT"
                detail = "; ".join(errors)
                if detail not in self.reason:
                    self.reason += "; " + detail

    def schedule(self, demand, now):
        with self.lock:
            self.account(now)
            if self.stopped.is_set():
                return
            if not math.isfinite(demand):
                self.shutdown("Invalid heater demand", now)
                return
            demand = clamp(demand, 0.0, float(len(self.channels)))
            self.last_demand = demand
            if demand == 0 or demand == len(self.channels):
                # Do not repay old duty errors at a rail (especially never add
                # heat to repay a debt after the controller has requested zero).
                self.energy_error = 0.0
            if now < self.next_slot:
                return
            self.next_slot = now + SCHEDULE_INTERVAL_SECONDS
            # Accumulate REQUESTED minus DELIVERED heater-seconds, not slots.
            # Round the next slot's combined energy, never the bare PI output.
            desired = int(clamp(math.floor(demand + self.energy_error /
                                          SCHEDULE_INTERVAL_SECONDS + 0.5),
                                0, len(self.channels)))
            eligible = [ch for ch in self.channels
                        if self.last_transition[ch] is None
                        or now - self.last_transition[ch] >= MIN_DWELL_SECONDS]
            off = sorted((ch for ch in eligible if not self.on[ch]),
                         key=lambda ch: (self.on_seconds[ch], ch))
            on = sorted((ch for ch in eligible if self.on[ch]),
                        key=lambda ch: (-self.on_seconds[ch], ch))
            delta = desired - sum(self.on.values())
            try:
                if delta < 0:
                    for ch in on[: -delta]:
                        self.change(ch, False, now)
                elif delta > 0:
                    for ch in off[:delta]:
                        self.change(ch, True, now)
                elif (on and off and now - self.last_rotation >= ROTATION_SECONDS
                      and self.on_seconds[on[0]] - self.on_seconds[off[0]]
                      >= ROTATION_SECONDS):
                    # One swap only when count is steady and imbalance warrants
                    # the extra two transitions. Always switch OFF before ON.
                    self.change(on[0], False, now)
                    self.change(off[0], True, now)
                    self.last_rotation = now
            except Exception as error:
                self.shutdown(f"Relay communication failure: {error}", now)
            self.next_slot = max(self.next_slot, max(
                (t + MIN_DWELL_SECONDS for t in self.last_transition.values()
                 if t is not None), default=now))


def create_log(target, channels, root=LOG_DIR, hardware=True):
    started = datetime.now().astimezone()
    folder = root / started.strftime("%Y-%m-%d_%H-%M-%S_%f")
    folder.mkdir(parents=True)
    config = dict(started_at=started.isoformat(), target_C=target,
                  mode="hardware" if hardware else "simulation",
                  heater_serial=HEATER_SERIAL, heater_channels=channels,
                  sensor_serial=SENSOR_SERIAL, sensor_channels=SENSOR_CHANNELS,
                  Kp=KP, Ki=KI, Kd=0, Kp_units="heater equivalents / predicted C",
                  Ki_units="heater equivalents / (predicted C * second)",
                  lookahead_seconds=LOOKAHEAD_SECONDS, rate_filter_seconds=RATE_FILTER_SECONDS,
                  rate_compensation_gain_heater_s_per_C=KP * LOOKAHEAD_SECONDS,
                  integral_band_C=INTEGRAL_BAND_C,
                  max_sensor_spread_C=MAX_SENSOR_SPREAD_C,
                  max_safety_temperature_C=MAX_SAFETY_TEMPERATURE_C,
                  log_interval_seconds=LOG_INTERVAL_SECONDS,
                  sensor_poll_interval_seconds=LOOP_INTERVAL_SECONDS,
                  temperature_average_seconds=TEMPERATURE_AVERAGE_SECONDS,
                  sensor_data_interval_policy="minimum supported by each channel",
                  schedule_interval_seconds=SCHEDULE_INTERVAL_SECONDS,
                  min_dwell_seconds=MIN_DWELL_SECONDS,
                  rotation_seconds=ROTATION_SECONDS,
                  control_strategy="predictive PI with integral separation",
                  simulation_plant=dict(heat_rate=0.0281, loss=0.000875, lag=70.0, ambient=21.8))
    (folder / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return folder


def log_fields(channels):
    return ["timestamp", *[f"TI_{ch}_C" for ch in SENSOR_CHANNELS], "avg_C",
            "target_C", "pi_output", "heaters_on_count",
            *[f"heater_{ch}_on" for ch in SENSOR_CHANNELS],
            *[f"heater_{ch}_on_seconds" for ch in channels],
            *[f"heater_{ch}_transitions" for ch in channels],
            *[f"heater_{ch}_last_transition_monotonic" for ch in channels],
            "controller_state", "fault_reason", "control_mode",
            "rate_C_per_s", "predicted_C", "integral_output", "duty_error_heater_seconds",
            *[f"raw_TI_{ch}_C" for ch in SENSOR_CHANNELS]]


def write_sample(writer, log, relays, temps, target, output, now, controller=None,
                 raw_temps=None):
    with relays.lock:
        relays.account(now)
        # Invalid readings remain in terminal fault rows; absent = blank.
        values = list(temps) if temps is not None else []
        raw_values = list(raw_temps) if raw_temps is not None else []
        average = (sum(values) / 4 if len(values) == 4 and all(
            isinstance(v, (int, float)) and math.isfinite(v) for v in values) else "")
        writer.writerow([datetime.now().astimezone().isoformat(),
                         *(values + [""] * 4)[:4], average, target, output,
                         sum(relays.on.values()),
                         *[int(relays.on.get(ch, False)) for ch in SENSOR_CHANNELS],
                         *[round(relays.on_seconds[ch], 3) for ch in relays.channels],
                         *[relays.transitions[ch] for ch in relays.channels],
                         *[relays.last_transition[ch] for ch in relays.channels],
                         relays.state, relays.reason,
                         controller.mode if controller else "",
                         controller.rate if controller else "",
                         controller.predicted if controller else "",
                         controller.integral if controller else "", relays.energy_error,
                         *(raw_values + [""] * 4)[:4]])
        log.flush()


def attach_hardware(relays, sensors):
    # Lazy import: unit tests can mock device attachment without loading Phidgets.
    from Phidget22.Devices.DigitalOutput import DigitalOutput
    from Phidget22.Devices.TemperatureSensor import TemperatureSensor

    def on_error(device, code, description):
        if not relays.stopped.is_set():
            relays.shutdown(f"Phidgets error {code}: {description}")

    def on_detach(device):
        if not relays.stopped.is_set():
            relays.shutdown("Phidget disconnected")

    for ch in relays.channels:
        if relays.stopped.is_set():
            raise RuntimeError(relays.reason)
        device = DigitalOutput()
        relays.outputs[ch] = device  # Track before opening for partial-setup cleanup.
        device.setDeviceSerialNumber(HEATER_SERIAL)
        device.setChannel(ch)
        device.setOnErrorHandler(on_error)
        device.setOnDetachHandler(on_detach)
        device.openWaitForAttachment(ATTACH_TIMEOUT_MS)
        device.setState(False)
    sensor_intervals = {}
    for ch in SENSOR_CHANNELS:
        if relays.stopped.is_set():
            raise RuntimeError(relays.reason)
        device = TemperatureSensor()
        sensors.append(device)
        device.setDeviceSerialNumber(SENSOR_SERIAL)
        device.setChannel(ch)
        device.setOnErrorHandler(on_error)
        device.setOnDetachHandler(on_detach)
        device.openWaitForAttachment(ATTACH_TIMEOUT_MS)
        device.setDataInterval(device.getMinDataInterval())
        sensor_intervals[ch] = device.getDataInterval()
    return sensor_intervals

def poll_stop(relays, command):
    """Read available console input without a blocking background input()."""
    if sys.platform == "win32":
        import msvcrt

        for _ in range(128):  # Bound work so keyboard traffic cannot stall control.
            if not msvcrt.kbhit():
                break
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):  # Ignore arrow/function-key scan codes.
                msvcrt.getwch()
            elif key in ("\r", "\n"):
                print()
                if command.strip().upper() == "STOP":
                    relays.shutdown("Manual STOP", fault=False)
                    return ""
                command = ""
            elif key == "\b":
                if command:
                    command = command[:-1]
                    print("\b \b", end="", flush=True)
            elif key.isprintable() and len(command) < 128:
                command += key
                print(key, end="", flush=True)
    else:
        import select

        if select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if not line:
                relays.shutdown("Console input closed", fault=False)
            elif line.strip().upper() == "STOP":
                relays.shutdown("Manual STOP", fault=False)
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--simulate", action="store_true",
                      help="simulate temperatures and relays without accessing Phidgets")
    mode.add_argument("--hardware", dest="simulate", action="store_false",
                      help="actuate the existing Phidgets (the default)")
    parser.set_defaults(simulate=False)
    args = parser.parse_args(argv)
    target = float(input("Target temperature (C): "))
    if not math.isfinite(target) or target > MAX_SAFETY_TEMPERATURE_C:
        raise ValueError("Target must be finite and at or below safety temperature")
    relays = Relays(HEATER_CHANNELS, monotonic())
    controller = Controller(len(HEATER_CHANNELS))
    sensors = []
    temps, output = None, 0.0
    raw_temps = None
    history = deque()
    log = writer = None
    try:
        folder = create_log(target, HEATER_CHANNELS, hardware=not args.simulate)
        log = (folder / "data.csv").open("w", newline="", encoding="utf-8")
        writer = csv.writer(log)
        writer.writerow(log_fields(HEATER_CHANNELS))
        log.flush()
        print(f"{'SIMULATION' if args.simulate else 'HARDWARE'} | Logging to {folder}")
        print("Type STOP + Enter or press Ctrl+C to stop.")
        if not args.simulate:
            intervals = attach_hardware(relays, sensors)
            if intervals is not None:
                config_path = folder / "config.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["sensor_data_intervals_ms"] = intervals
                config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        simulated, heat, previous_time = 21.8, 0.0, monotonic()
        command = ""
        next_update = monotonic()
        while not relays.stopped.is_set():
            command = poll_stop(relays, command)
            if relays.stopped.is_set():
                break
            now = monotonic()
            if not args.simulate:
                raw_temps = None  # Avoid logging stale raw values on a read failure.
                raw_temps = [sensor.getTemperature() for sensor in sensors]
            else:
                simulated, heat = thermal_step(simulated, heat, sum(relays.on.values()),
                                               now - previous_time)
                raw_temps = [simulated] * 4
            previous_time = now
            validate_temperatures(raw_temps)  # Raw safety BEFORE smoothing, every poll.
            history.append((now, tuple(raw_temps)))
            update = now >= next_update
            if update:
                temps = average_temperatures(history, now)
                average = sum(temps) / 4
                output = controller.update(target, average, now)
                next_update = now + LOG_INTERVAL_SECONDS
            relays.schedule(output, now)
            if update:
                write_sample(writer, log, relays, temps, target, output, now, controller,
                             raw_temps=raw_temps)
                print(f"Temps {'/'.join(f'{v:.1f}' for v in temps)} | Average {average:.1f}"
                      f" | Target {target:.1f} | Demand {output:.2f}"
                      f" | Heaters ON {sum(relays.on.values())}"
                      f" {tuple(ch for ch in relays.channels if relays.on[ch])}")
            relays.stopped.wait(LOOP_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        relays.shutdown("Ctrl+C", fault=False)
    except Exception as error:
        relays.shutdown(f"{type(error).__name__}: {error}")
    finally:
        relays.shutdown("Controller shutdown", fault=False)
        # OFF and close every handle even when another handle or logging fails.
        try:
            if writer is not None:
                write_sample(writer, log, relays, temps, target, output, monotonic(), controller,
                             raw_temps=raw_temps)
        finally:
            try:
                if log is not None:
                    log.close()
            finally:
                for device in [*relays.outputs.values(), *sensors]:
                    try:
                        device.close()
                    except Exception as error:
                        print(f"Device close failed: {error}")
        print(f"{relays.state}: {relays.reason}")
    return 1 if relays.state == "FAULT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
