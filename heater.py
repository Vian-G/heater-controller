"""PI temperature controller for the existing Phidget heater hardware.

Operation requires inspected mains wiring/protection and an independent
hardware overtemperature cutoff. Software cannot protect against
welded contacts, loss of USB, or computer failure. No automatic fault recovery.
"""

import csv
import json
import math
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic

HEATER_SERIAL = 343887
SENSOR_SERIAL = 341989
HEATER_CHANNELS = (0, 1, 2)  # Any nonempty subset of (0, 1, 2, 3).
SENSOR_CHANNELS = (0, 1, 2, 3)
KP = 0.10  # Heater equivalents / degree C.
KI = 0.0005  # Heater equivalents / (degree C * second). Kd = 0 (PI only).
# Provisional, NOT validated to +/-2 C; no November 14 test data is bundled.
MAX_SAFETY_TEMPERATURE_C = 150.0  # Review for actual box/materials before use.
MAX_SENSOR_SPREAD_C = 30.0
LOG_INTERVAL_SECONDS = 5.0  # Also the PI update interval.
SCHEDULE_INTERVAL_SECONDS = 60.0
MIN_DWELL_SECONDS = 60.0
ROTATION_SECONDS = 600.0  # Rotate steady integer loads at most once per 10 min.
INTEGER_TOLERANCE = 0.01  # Heater equivalents; suppress tiny fractional dithering.
LOOP_INTERVAL_SECONDS = 0.25  # Sensor checks independent of PI/relay slots.
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


class PI:
    def __init__(self, count, kp=KP, ki=KI):
        self.count, self.kp, self.ki = count, kp, ki
        self.integral = 0.0  # Heater equivalents, bounded to [0, N].
        self.last_time = None

    def update(self, target, average, now):
        if not all(math.isfinite(v) for v in (target, average, now)):
            raise ValueError("Nonfinite PI input")
        dt = 0.0 if self.last_time is None else max(0.0, now - self.last_time)
        self.last_time = now
        error = target - average
        proportional = self.kp * error
        raw = proportional + self.integral
        # Freeze integration when error pushes farther into a rail; permit
        # unwinding in the opposite direction. Bound the integral as well.
        if not ((raw >= self.count and error > 0) or (raw <= 0 and error < 0)):
            self.integral = clamp(self.integral + self.ki * error * dt, 0, self.count)
        return clamp(proportional + self.integral, 0.0, float(self.count))


class Relays:
    """60-second pulse-density slots, per-channel dwell, runtime balancing.

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
        self.last_transition = {ch: now for ch in channels}
        self.accounted_at = now
        self.next_slot = now + MIN_DWELL_SECONDS  # Conservative startup OFF dwell.
        self.last_rotation = now
        self.fraction = 0.0
        self.state, self.reason = "RUNNING", ""
        self.shutting_down = False
        self.lock = threading.RLock()
        self.stopped = threading.Event()

    def account(self, now):
        # A callback may have accounted a slightly later timestamp already.
        now = max(now, self.accounted_at)
        for ch in self.channels:
            if self.on[ch]:
                self.on_seconds[ch] += now - self.accounted_at
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
            if now < self.next_slot:
                return
            # Never replay missed slots after a delayed loop.
            self.next_slot = now + SCHEDULE_INTERVAL_SECONDS
            demand = clamp(demand, 0.0, float(len(self.channels)))
            nearest = round(demand)
            if abs(demand - nearest) <= INTEGER_TOLERANCE:
                desired = nearest
                self.fraction = 0.0
            else:
                desired = math.floor(demand)
                self.fraction += demand - desired
                if self.fraction >= 1.0 - 1e-9:
                    desired += 1
                    self.fraction = max(0.0, self.fraction - 1.0)
            eligible = [ch for ch in self.channels
                        if now - self.last_transition[ch] >= MIN_DWELL_SECONDS]
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
            self.next_slot = max(self.next_slot,
                                 max(self.last_transition.values()) + MIN_DWELL_SECONDS)


def create_log(target, channels, root=LOG_DIR):
    started = datetime.now().astimezone()
    folder = root / started.strftime("%Y-%m-%d_%H-%M-%S_%f")
    folder.mkdir(parents=True)
    config = dict(started_at=started.isoformat(), target_C=target, mode="hardware",
                  heater_serial=HEATER_SERIAL, heater_channels=channels,
                  sensor_serial=SENSOR_SERIAL, sensor_channels=SENSOR_CHANNELS,
                  Kp=KP, Ki=KI, Kd=0, Kp_units="heater equivalents / C",
                  Ki_units="heater equivalents / (C * second)",
                  max_sensor_spread_C=MAX_SENSOR_SPREAD_C,
                  max_safety_temperature_C=MAX_SAFETY_TEMPERATURE_C,
                  log_interval_seconds=LOG_INTERVAL_SECONDS,
                  schedule_interval_seconds=SCHEDULE_INTERVAL_SECONDS,
                  min_dwell_seconds=MIN_DWELL_SECONDS,
                  rotation_seconds=ROTATION_SECONDS,
                  integer_tolerance=INTEGER_TOLERANCE)
    (folder / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return folder


def log_fields(channels):
    return ["timestamp", *[f"TI_{ch}_C" for ch in SENSOR_CHANNELS], "avg_C",
            "target_C", "pi_output", "heaters_on_count",
            *[f"heater_{ch}_on" for ch in SENSOR_CHANNELS],
            *[f"heater_{ch}_on_seconds" for ch in channels],
            *[f"heater_{ch}_transitions" for ch in channels],
            *[f"heater_{ch}_last_transition_monotonic" for ch in channels],
            "controller_state", "fault_reason"]


def write_sample(writer, log, relays, temps, target, output, now):
    with relays.lock:
        relays.account(now)
        # Invalid readings remain in terminal fault rows; absent = blank.
        values = list(temps) if temps is not None else []
        average = (sum(values) / 4 if len(values) == 4 and all(
            isinstance(v, (int, float)) and math.isfinite(v) for v in values) else "")
        writer.writerow([datetime.now().astimezone().isoformat(),
                         *(values + [""] * 4)[:4], average, target, output,
                         sum(relays.on.values()),
                         *[int(relays.on.get(ch, False)) for ch in SENSOR_CHANNELS],
                         *[round(relays.on_seconds[ch], 3) for ch in relays.channels],
                         *[relays.transitions[ch] for ch in relays.channels],
                         *[relays.last_transition[ch] for ch in relays.channels],
                         relays.state, relays.reason])
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
        relays.last_transition[ch] = monotonic()
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

    relays.next_slot = max(relays.last_transition.values()) + MIN_DWELL_SECONDS


def console_stop(relays):
    try:
        while not relays.stopped.is_set():
            if input().strip().upper() == "STOP":
                relays.shutdown("Manual STOP", fault=False)
    except (EOFError, OSError):
        relays.shutdown("Console input closed", fault=False)


def main():
    target = float(input("Target temperature (C): "))
    if not math.isfinite(target) or target > MAX_SAFETY_TEMPERATURE_C:
        raise ValueError("Target must be finite and at or below safety temperature")
    relays = Relays(HEATER_CHANNELS, monotonic())
    pi = PI(len(HEATER_CHANNELS))
    sensors = []
    temps, output = None, 0.0
    log = writer = None
    try:
        folder = create_log(target, HEATER_CHANNELS)
        log = (folder / "data.csv").open("w", newline="", encoding="utf-8")
        writer = csv.writer(log)
        writer.writerow(log_fields(HEATER_CHANNELS))
        log.flush()
        print(f"HARDWARE | Logging to {folder}")
        print("Type STOP + Enter or press Ctrl+C to stop. Initial OFF dwell: 60 s.")
        attach_hardware(relays, sensors)
        threading.Thread(target=console_stop, args=(relays,), daemon=True).start()
        next_update = monotonic()
        while not relays.stopped.is_set():
            now = monotonic()
            temps = None  # Avoid logging stale values if the read fails.
            temps = [sensor.getTemperature() for sensor in sensors]
            average = validate_temperatures(temps)  # Every loop, not every slot.
            update = now >= next_update
            if update:
                output = pi.update(target, average, now)
                next_update = now + LOG_INTERVAL_SECONDS
            relays.schedule(output, now)
            if update:
                write_sample(writer, log, relays, temps, target, output, now)
                print(f"Temps {'/'.join(f'{v:.1f}' for v in temps)} | Average {average:.1f}"
                      f" | Target {target:.1f} | PI {output:.2f}"
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
                write_sample(writer, log, relays, temps, target, output, monotonic())
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
