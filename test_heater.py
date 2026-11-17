"""Deterministic stdlib tests. All outputs are mocks; no Phidgets imports."""
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import heater as h


class Output:
    def __init__(self):
        self.commands = []
        self.fail = False
        self.closed = False

    def setState(self, state):
        self.commands.append(state)
        if self.fail:
            raise OSError("USB failure")

    def close(self):
        self.closed = True


def make_relays(channels):
    outputs = {ch: Output() for ch in channels}
    return h.Relays(channels, 0, outputs, clock=lambda: 0.0)


class ControllerTests(unittest.TestCase):
    @patch.object(h, "TEMPERATURE_AVERAGE_SECONDS", 3.0)
    def test_three_second_average_with_jitter_and_partial_startup(self):
        from collections import deque
        history = deque([(0.0, (10, 20, 30, 40))])
        self.assertEqual(h.average_temperatures(history, 0), [10, 20, 30, 40])
        history.extend([(0.9, (20, 30, 40, 50)), (2.7, (30, 40, 50, 60))])
        self.assertEqual(h.average_temperatures(history, 2.7), [20, 30, 40, 50])
        self.assertEqual(h.average_temperatures(history, 3.0), [25, 35, 45, 55])
        self.assertEqual(h.average_temperatures(history, 4.0), [30, 40, 50, 60])
        with self.assertRaises(ValueError):
            h.average_temperatures(history, 6.0)

    def test_raw_safety_fault_cannot_be_hidden_by_average(self):
        from collections import deque
        for bad in ([151] * 4, [20, 20, 20, 51], [float("nan")] * 4):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                clock = [0.0]
                captured = []

                class Sensor(Output):
                    def __init__(self, ch):
                        super().__init__()
                        self.ch = ch

                    def getTemperature(self):
                        return bad[self.ch] if clock[0] >= 0.1 else 20.0

                def setup(relays, sensors):
                    captured.append(relays)
                    relays.outputs.update({ch: Output() for ch in relays.channels})
                    sensors.extend(Sensor(ch) for ch in range(4))
                    relays.stopped.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

                with patch.object(h, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(h, "attach_hardware", setup), \
                        patch.object(h, "create_log", return_value=Path(tmp)), \
                        patch.object(h, "poll_stop", side_effect=lambda r, command: command), \
                        patch("builtins.input", return_value="100"), patch("builtins.print"):
                    self.assertEqual(h.main([]), 1)
                self.assertEqual(clock[0], 0.1)  # Before next 5 s control update.
                self.assertFalse(any(captured[0].on.values()))
                self.assertTrue(all(v == 0.1 for v in captured[0].on_seconds.values()))
                with (Path(tmp) / "data.csv").open() as log:
                    rows = list(csv.DictReader(log))
                self.assertEqual(rows[-1]["controller_state"], "FAULT")
                self.assertEqual(rows[-1]["raw_TI_0_C"], str(bad[0]))

    def test_subsets_and_integer_demands(self):
        from itertools import combinations
        for count in range(1, 5):
            for channels in combinations(range(4), count):
                for demand in range(count + 1):
                    with self.subTest(channels=channels, demand=demand):
                        r = make_relays(channels)
                        r.schedule(demand, 0)
                        self.assertEqual(sum(r.on.values()), demand)
                        for now in range(60, 3601, 60):
                            r.schedule(demand, now)
                            self.assertEqual(sum(r.on.values()), demand)
                        if demand in (0, count):
                            self.assertEqual(sum(r.transitions.values()), demand)

    def test_fractional_demands_and_fairness(self):
        for count in range(1, 5):
            for demand in (0.5, 1.5, 2.5, 3.7):
                if demand > count:
                    continue
                with self.subTest(count=count, demand=demand):
                    r = make_relays(tuple(range(count)))
                    delivered = 0
                    for now in range(60, 36001, 60):
                        r.schedule(demand, now)
                        actual = sum(r.on.values())
                        self.assertIn(actual, (int(demand), int(demand) + 1))
                        delivered += actual
                    self.assertAlmostEqual(delivered / 600, demand, places=8)
                    r.account(36060)
                    runtimes = list(r.on_seconds.values())
                    self.assertGreater(min(runtimes), 0)
                    self.assertLessEqual(max(runtimes) - min(runtimes), 660)

    def test_integer_rotation_and_near_integer_stability(self):
        r = make_relays((0, 1, 2, 3))
        for now in range(60, 36001, 60):
            r.schedule(2 + (0.005 if now % 120 else -0.005), now)
            self.assertEqual(sum(r.on.values()), 2)
        r.account(36060)
        self.assertLessEqual(max(r.on_seconds.values()) - min(r.on_seconds.values()), 660)
        self.assertLess(sum(r.transitions.values()), 150)

    def test_dwell_rapid_targets_and_delayed_slots(self):
        r = make_relays((0, 1, 2, 3))
        pi = h.Controller(4)
        previous = dict(r.on)
        last = {ch: -60 for ch in r.channels}
        for now in range(1, 3601):
            target = 120 if now % 17 < 8 else 10
            demand = pi.update(target, h.validate_temperatures([50] * 4), now)
            r.schedule(demand, now)
            for ch in r.channels:
                if r.on[ch] != previous[ch]:
                    self.assertGreaterEqual(now - last[ch], 30)
                    last[ch] = now
            previous = dict(r.on)
        r = make_relays((0,))
        for now, demand, expected in ((60, 1, True), (61, 0, True),
                                      (89.9, 0, True), (90, 0, False),
                                      (91, 1, False), (120, 1, True),
                                      (1000, 0, False), (1001, 1, False),
                                      (1030, 1, True)):
            r.schedule(demand, now)
            self.assertEqual(r.on[0], expected)

    def test_prediction_warmup_and_antiwindup(self):
        control = h.Controller(3)
        for now in range(0, 1000, 5):
            self.assertEqual(control.update(100, 20, now), 3)
            self.assertEqual(control.integral, 0)
        control = h.Controller(3)
        before = control.update(50, 40, 0)
        after = control.update(50, 40.4, 5)
        self.assertLess(after, before)  # Rising measurement cuts power early.
        self.assertGreater(control.predicted, 40.4)
        control.integral = 3
        self.assertEqual(control.update(50, 55, 10), 0)
        self.assertLess(control.integral, 3)  # Unwind even while output is zero.
        flat = h.Controller(3)
        flat.update(50, 40, 0)
        flat.update(100, 40, 7)
        self.assertEqual(flat.rate, 0)  # No derivative kick on setpoint change.
        self.assertEqual(flat.integral, 0)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                h.Controller(3).update(value, 20, 0)
        with self.assertRaises(ValueError):
            flat.update(50, 40, 7)

    def test_emergency_shutdown_and_latch(self):
        for reason in ("Emergency stop", "Sensor disconnected", "Manual STOP"):
            r = make_relays((0, 1, 2, 3))
            r.schedule(4, 60)
            r.shutdown(reason, 61)
            self.assertFalse(any(r.on.values()))
            self.assertTrue(all(r.on_seconds[ch] == 1 for ch in r.channels))
            r.schedule(4, 6000)
            r.shutdown("second reason", 6001)
            self.assertEqual(r.reason, reason)
            self.assertFalse(any(r.on.values()))
            self.assertTrue(all(not device.commands[-1] for device in r.outputs.values()))
        # A fresh invocation can heat on its first valid demand without a delay.
        fresh = make_relays((0,))
        fresh.schedule(1, 0)
        self.assertTrue(fresh.on[0])

    def test_shutdown_attempts_other_outputs_after_failure(self):
        r = make_relays((0, 1, 2))
        r.schedule(3, 60)
        r.outputs[0].fail = True
        r.shutdown("Emergency", 61)
        self.assertEqual(r.state, "FAULT")
        self.assertIn("OFF failed for heater 0", r.reason)
        self.assertFalse(r.on[1] or r.on[2])
        self.assertFalse(r.outputs[0].commands[-1])
        # Failure on an ON command also latches and shuts down the other outputs.
        r = make_relays((0, 1, 2))
        r.outputs[1].fail = True
        r.schedule(3, 60)
        self.assertEqual(r.state, "FAULT")
        self.assertFalse(r.on[0] or r.on[2])

    def test_invalid_sensor_readings_latch(self):
        invalid = (None, [], [20] * 3, [20] * 5, [None, 20, 20, 20],
                   ["20", 20, 20, 20], [True, 20, 20, 20],
                   [float("nan"), 20, 20, 20], [float("inf"), 20, 20, 20],
                   [-float("inf"), 20, 20, 20], [20, 20, 20, 51], [151] * 4)
        for values in invalid:
            with self.subTest(values=values):
                r = make_relays((0,))
                r.schedule(1, 60)
                try:
                    h.validate_temperatures(values)
                except ValueError as error:
                    r.shutdown(str(error), 61)
                self.assertEqual(r.state, "FAULT")
                self.assertFalse(r.on[0])
                h.validate_temperatures([20] * 4)
                r.schedule(1, 120)
                self.assertFalse(r.on[0])
        self.assertEqual(h.validate_temperatures([20, 30, 40, 50]), 35)

    def test_csv_and_json(self):
        for channels in ((2,), (0, 3), (0, 1, 2), (0, 1, 2, 3)):
            with tempfile.TemporaryDirectory() as tmp:
                folder = h.create_log(80, channels, Path(tmp), hardware=True)
                config = json.loads((folder / "config.json").read_text())
                self.assertEqual(config["heater_serial"], 343887)
                self.assertEqual(config["sensor_serial"], 341989)
                self.assertEqual(config["heater_channels"], list(channels))
                self.assertEqual(config["mode"], "hardware")
                self.assertNotIn("dry_run", config)
                self.assertEqual(config["min_dwell_seconds"], 30)
                self.assertEqual(config["log_interval_seconds"], 5)
                self.assertEqual(config["temperature_average_seconds"], h.TEMPERATURE_AVERAGE_SECONDS)
                self.assertEqual(config["sensor_poll_interval_seconds"], .1)
                r = make_relays(channels)
                r.schedule(len(channels), 60)
                log = io.StringIO()
                writer = csv.writer(log)
                writer.writerow(h.log_fields(channels))
                h.write_sample(writer, log, r, [20, 21, 22, 23], 80, 1.5, 65)
                r.shutdown("sensor fault", 66)
                h.write_sample(writer, log, r, None, 80, 1.5, 66)
                rows = list(csv.DictReader(io.StringIO(log.getvalue())))
                self.assertNotIn(None, rows[0])
                self.assertEqual(rows[0]["avg_C"], "21.5")
                self.assertEqual(rows[0]["heaters_on_count"], str(len(channels)))
                for ch in channels:
                    self.assertEqual(float(rows[0][f"heater_{ch}_on_seconds"]), 5)
                self.assertEqual(rows[1]["controller_state"], "FAULT")
                self.assertEqual(rows[1]["heaters_on_count"], "0")
                self.assertEqual(rows[1]["fault_reason"], "sensor fault")
                self.assertEqual(rows[1]["TI_0_C"], "")
                from datetime import datetime
                self.assertIsNotNone(datetime.fromisoformat(rows[0]["timestamp"]).tzinfo)

    def test_partial_setup_cleanup_in_main(self):
        outputs, sensors = [Output(), Output()], [Output()]

        def fail_setup(relays, attached_sensors):
            relays.outputs.update(enumerate(outputs))
            attached_sensors.extend(sensors)
            raise OSError("attach failed")

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "run"
            folder.mkdir()
            with patch.object(h, "attach_hardware", fail_setup), \
                    patch.object(h, "create_log", return_value=folder), \
                    patch("builtins.input", return_value="80"), \
                    patch("builtins.print"):
                self.assertEqual(h.main([]), 1)
            self.assertTrue(all(device.closed for device in outputs + sensors))
            self.assertTrue(all(device.commands[-1] is False for device in outputs))
            with (folder / "data.csv").open() as log:
                row = list(csv.DictReader(log))[-1]
            self.assertIn("attach failed", row["fault_reason"])

    def test_main_sensor_fault_and_ctrl_c_while_heating(self):
        for failure in (float("nan"), OSError("thermocouple disconnected"), KeyboardInterrupt()):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                clock = [0.0]
                captured = []
                outputs = {ch: Output() for ch in h.HEATER_CHANNELS}

                class Sensor(Output):
                    def getTemperature(self):
                        if clock[0] >= 65:
                            if isinstance(failure, BaseException):
                                raise failure
                            return failure
                        return 20.0

                sensors = [Sensor() for _ in range(4)]

                def setup(relays, attached):
                    captured.append(relays)
                    relays.outputs.update(outputs)
                    attached.extend(sensors)
                    relays.stopped.wait = lambda seconds: clock.__setitem__(0, clock[0] + 5)

                folder = Path(tmp)
                with patch.object(h, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(h, "attach_hardware", setup), \
                        patch.object(h, "create_log", return_value=folder), \
                        patch.object(h, "poll_stop", side_effect=lambda r, command: command), \
                        patch("builtins.input", return_value="100"), patch("builtins.print"):
                    result = h.main([])
                r = captured[0]
                self.assertEqual(result, 0 if isinstance(failure, KeyboardInterrupt) else 1)
                self.assertEqual(clock[0], 65)
                self.assertTrue(all(r.on_seconds[ch] == 65 for ch in r.channels))
                self.assertFalse(any(r.on.values()))
                self.assertTrue(all(device.closed for device in [*outputs.values(), *sensors]))
                self.assertTrue(all(device.commands[-1] is False for device in outputs.values()))

    def test_error_callback_during_relay_write(self):
        r = make_relays((0, 1, 2))
        original = r.outputs[0].setState

        def callback_write(state):
            original(state)
            r.shutdown("Phidgets error callback", 60)

        r.outputs[0].setState = callback_write
        r.schedule(3, 60)
        self.assertEqual(r.state, "FAULT")
        self.assertFalse(any(r.on.values()))
        self.assertTrue(all(device.commands[-1] is False for device in r.outputs.values()))

    def test_windows_console_stop(self):
        from types import SimpleNamespace
        r = make_relays((0,))
        r.schedule(1, 60)
        keys = list(" stox\bp \r")
        console = SimpleNamespace(kbhit=lambda: bool(keys), getwch=lambda: keys.pop(0))
        with patch.dict(sys.modules, {"msvcrt": console}), \
                patch.object(h.sys, "platform", "win32"), \
                patch.object(h, "monotonic", return_value=61), patch("builtins.print"), \
                patch("builtins.input", side_effect=AssertionError("Must not call input")):
            h.poll_stop(r, "")
        self.assertEqual(r.state, "STOPPED")
        self.assertFalse(r.on[0])

    def test_windows_console_idle_and_partial_command(self):
        from types import SimpleNamespace
        r = make_relays((0,))
        keys = []
        console = SimpleNamespace(kbhit=lambda: bool(keys), getwch=lambda: keys.pop(0))
        with patch.dict(sys.modules, {"msvcrt": console}), \
                patch.object(h.sys, "platform", "win32"), patch("builtins.print"), \
                patch("builtins.input", side_effect=EOFError):
            for now in range(0, 121, 5):
                self.assertEqual(h.poll_stop(r, ""), "")
                r.schedule(1, now)
            self.assertEqual(r.state, "RUNNING")
            self.assertTrue(r.on[0])
            keys.extend("st")
            command = h.poll_stop(r, "")
            self.assertEqual(command, "st")
            keys.extend("op\r")
            with patch.object(h, "monotonic", return_value=121):
                h.poll_stop(r, command)
            self.assertFalse(r.on[0])

    def test_phidget_configuration_and_error_detach_callbacks(self):
        from types import SimpleNamespace

        class Device(Output):
            def setDeviceSerialNumber(self, serial):
                self.serial = serial

            def setChannel(self, channel):
                self.channel = channel

            def setOnErrorHandler(self, callback):
                self.error = callback

            def setOnDetachHandler(self, callback):
                self.detach = callback

            def openWaitForAttachment(self, timeout):
                self.timeout = timeout

            def getMinDataInterval(self):
                return 32

            def setDataInterval(self, interval):
                self.interval = interval

            def getDataInterval(self):
                return self.interval

        modules = {
            'Phidget22.Devices.DigitalOutput': SimpleNamespace(DigitalOutput=Device),
            'Phidget22.Devices.TemperatureSensor': SimpleNamespace(TemperatureSensor=Device),
        }
        for event in ('error', 'detach'):
            r = make_relays((0, 2))
            sensors = []
            with patch.dict(sys.modules, modules), patch.object(h, 'monotonic', return_value=0):
                intervals = h.attach_hardware(r, sensors)
            self.assertEqual(intervals, {ch: 32 for ch in range(4)})
            self.assertTrue(all(d.interval == 32 for d in sensors))
            self.assertEqual(set(r.outputs), {0, 2})
            self.assertTrue(all(d.serial == 343887 and d.channel == ch
                                for ch, d in r.outputs.items()))
            self.assertEqual([d.channel for d in sensors], [0, 1, 2, 3])
            self.assertTrue(all(d.serial == 341989 for d in sensors))
            self.assertTrue(all(d.commands == [False] for d in r.outputs.values()))
            r.schedule(2, 60)
            with patch.object(h, 'monotonic', return_value=61):
                if event == 'error':
                    sensors[0].error(sensors[0], 123, 'sensor fault')
                else:
                    sensors[0].detach(sensors[0])
            self.assertEqual(r.state, 'FAULT')
            self.assertFalse(any(r.on.values()))
            r.schedule(2, 1000)
            self.assertFalse(any(r.on.values()))
            commands = [len(d.commands) for d in r.outputs.values()]
            sensors[0].detach(sensors[0])  # Cleanup detaches do not trigger writes.
            self.assertEqual(commands, [len(d.commands) for d in r.outputs.values()])

    def test_slow_usb_write_does_not_shorten_dwell(self):
        clock = [60.0]
        r = h.Relays((0,), 0, {0: Output()}, clock=lambda: clock[0])
        original = r.outputs[0].setState

        def slow_write(state):
            original(state)
            clock[0] += 2

        r.outputs[0].setState = slow_write
        r.schedule(1, 60)
        self.assertEqual(r.last_transition[0], 62)
        clock[0] = 90
        r.schedule(0, 90)
        self.assertTrue(r.on[0])
        clock[0] = 92
        r.schedule(0, 92)
        self.assertFalse(r.on[0])
        self.assertGreaterEqual(r.last_transition[0] - 62, 30)

    def test_explicit_simulation_never_attaches(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)

            clock = [0.0]

            def stop(relays, command):
                if clock[0] >= 65:
                    relays.shutdown("Manual STOP", fault=False)
                return ""

            with patch.object(h, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(h.threading.Event, "wait", side_effect=lambda seconds:
                                 clock.__setitem__(0, clock[0] + 5)), \
                    patch.object(h, "attach_hardware") as attach, \
                    patch.object(h, "create_log", return_value=folder) as create, \
                    patch.object(h, "poll_stop", side_effect=stop), \
                    patch("builtins.input", return_value="50"), patch("builtins.print"):
                self.assertEqual(h.main(["--simulate"]), 0)
            attach.assert_not_called()
            with (folder / "data.csv").open() as log:
                rows = list(csv.DictReader(log))
            self.assertEqual(rows[0]["heaters_on_count"], "3")
            self.assertEqual(rows[-1]["controller_state"], "STOPPED")
            self.assertGreater(float(rows[-2]["avg_C"]), float(rows[0]["avg_C"]))
            self.assertFalse(create.call_args.kwargs["hardware"])
            config = json.loads((h.create_log(50, (0, 1, 2), folder, hardware=False) / "config.json").read_text())
            self.assertEqual(config["mode"], "simulation")
        self.assertEqual(h.MAX_SAFETY_TEMPERATURE_C, 150)
        self.assertEqual(h.HEATER_CHANNELS, (0, 1, 2))
        self.assertFalse(any(name.startswith("Phidget22") for name in sys.modules))
        for channels in ((), (4,), (0, 0)):
            with self.assertRaises(ValueError):
                make_relays(channels)

    def test_unbiased_small_fractional_demands_with_jitter(self):
        for demand in (.001, .5, .999, 1.001, 1.5, 2.5, 3.7, 3.999):
            r = make_relays((0, 1, 2, 3))
            end = 36000
            now = 0.0
            while now < end:
                r.schedule(demand, now)
                now += 7.3  # Slots drift; energy must be based on elapsed time.
            r.account(end)
            delivered = sum(r.on_seconds.values())
            self.assertLessEqual(abs(delivered-demand*end), 60)
        r = make_relays((0, 1, 2))
        r.energy_error = 10000
        r.schedule(0, 0)
        self.assertFalse(any(r.on.values()))  # No debt repayment at zero demand.

    def test_closed_loop_improves_legacy_pi_with_real_relay_dwell(self):
        from validate_control import simulate
        old = simulate(duration=7200, strategy="legacy")
        revised = simulate(duration=7200)
        self.assertLess(revised["overshoot_C"], 2)
        self.assertLess(revised["overshoot_C"], old["overshoot_C"])
        self.assertLess(revised["settling_min"], old["settling_min"])
        self.assertLess(revised["max_tail_abs_error_C"], 2)
        self.assertGreaterEqual(revised["minimum_normal_dwell_s"], 30)
        disturbance = simulate(disturbance=True, noise=.08)
        self.assertLess(disturbance["max_tail_abs_error_C"], 2)
        failed_heater = simulate(disconnected=(0,))
        # A physically unplugged element is unobservable to software; do not
        # assert a +/-2 C promise in this degraded-hardware scenario.
        self.assertFalse(failed_heater["fault"])
        self.assertLess(failed_heater["max_tail_abs_error_C"], 3)
        self.assertGreaterEqual(failed_heater["minimum_normal_dwell_s"], 30)

    def test_safety_trip_latches_off_but_stored_heat_can_keep_rising(self):
        from validate_control import simulate
        result = simulate(target=150, model="fast_low_loss", duration=7200)
        self.assertIn("Maximum safety temperature", result["fault"])
        self.assertIsNotNone(result["fault_time_s"])
        self.assertEqual(result["final_heaters_on_count"], 0)
        self.assertGreater(result["peak_C"], 150)  # OFF cannot remove stored heat.
        self.assertIsNone(result["settling_min"])


if __name__ == "__main__":
    unittest.main()
