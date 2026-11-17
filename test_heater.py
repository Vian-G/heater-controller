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
    def test_subsets_and_integer_demands(self):
        from itertools import combinations
        for count in range(1, 5):
            for channels in combinations(range(4), count):
                for demand in range(count + 1):
                    with self.subTest(channels=channels, demand=demand):
                        r = make_relays(channels)
                        r.schedule(demand, 59.99)
                        self.assertEqual(sum(r.on.values()), 0)
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
        pi = h.PI(4)
        previous = dict(r.on)
        last = {ch: 0 for ch in r.channels}
        for now in range(1, 3601):
            target = 120 if now % 17 < 8 else 10
            demand = pi.update(target, h.validate_temperatures([50] * 4), now)
            r.schedule(demand, now)
            for ch in r.channels:
                if r.on[ch] != previous[ch]:
                    self.assertGreaterEqual(now - last[ch], 60)
                    last[ch] = now
            previous = dict(r.on)
        r = make_relays((0,))
        for now, demand, expected in ((60, 1, True), (61, 0, True),
                                      (119.9, 0, True), (120, 0, False),
                                      (121, 1, False), (180, 1, True),
                                      (1000, 0, False), (1001, 1, False),
                                      (1060, 1, True)):
            r.schedule(demand, now)
            self.assertEqual(r.on[0], expected)

    def test_pi_elapsed_time_saturation_and_antiwindup(self):
        pi = h.PI(3, kp=0.1, ki=0.01)
        self.assertEqual(pi.update(21, 20, 0), 0.1)
        self.assertAlmostEqual(pi.update(21, 20, 7), 0.17)
        for now in range(8, 10000):
            self.assertEqual(pi.update(100, 20, now), 3)
        self.assertAlmostEqual(pi.integral, 0.07)
        self.assertEqual(pi.update(0, 20, 10000), 0)
        self.assertAlmostEqual(pi.integral, 0.07)
        # Unwind a positive integral when error reverses within the rails.
        pi.integral = 2
        self.assertLess(pi.update(19, 20, 10005), 1.9)
        for now in range(10010, 11000, 5):
            pi.update(21, 20, now)
        self.assertTrue(0 <= pi.integral <= 3)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                pi.update(value, 20, 12000)

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
        # A fresh invocation starts OFF and must earn a new startup dwell.
        fresh = make_relays((0,))
        fresh.schedule(1, 0)
        self.assertFalse(fresh.on[0])

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
                folder = h.create_log(80, channels, Path(tmp))
                config = json.loads((folder / "config.json").read_text())
                self.assertEqual(config["heater_serial"], 343887)
                self.assertEqual(config["sensor_serial"], 341989)
                self.assertEqual(config["heater_channels"], list(channels))
                self.assertEqual(config["mode"], "hardware")
                self.assertNotIn("dry_run", config)
                self.assertEqual(config["min_dwell_seconds"], 60)
                self.assertEqual(config["log_interval_seconds"], 5)
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
                self.assertEqual(h.main(), 1)
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
                        patch.object(h.threading, "Thread"), \
                        patch("builtins.input", return_value="100"), patch("builtins.print"):
                    result = h.main()
                r = captured[0]
                self.assertEqual(result, 0 if isinstance(failure, KeyboardInterrupt) else 1)
                self.assertEqual(clock[0], 65)
                self.assertTrue(all(r.on_seconds[ch] == 5 for ch in r.channels))
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

    def test_console_stop(self):
        r = make_relays((0,))
        r.schedule(1, 60)
        with patch("builtins.input", side_effect=["anything", " stop "]), \
                patch.object(h, "monotonic", return_value=61):
            h.console_stop(r)
        self.assertEqual(r.state, "STOPPED")
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

        modules = {
            'Phidget22.Devices.DigitalOutput': SimpleNamespace(DigitalOutput=Device),
            'Phidget22.Devices.TemperatureSensor': SimpleNamespace(TemperatureSensor=Device),
        }
        for event in ('error', 'detach'):
            r = make_relays((0, 2))
            sensors = []
            with patch.dict(sys.modules, modules), patch.object(h, 'monotonic', return_value=0):
                h.attach_hardware(r, sensors)
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
        clock[0] = 120
        r.schedule(0, 120)
        self.assertTrue(r.on[0])
        clock[0] = 122
        r.schedule(0, 122)
        self.assertFalse(r.on[0])
        self.assertGreaterEqual(r.last_transition[0] - 62, 60)

    def test_default_and_no_hardware_dependency(self):
        self.assertFalse(hasattr(h, "DRY_RUN"))
        self.assertEqual(h.HEATER_CHANNELS, (0, 1, 2))
        self.assertFalse(any(name.startswith("Phidget22") for name in sys.modules))
        for channels in ((), (4,), (0, 0)):
            with self.assertRaises(ValueError):
                make_relays(channels)


if __name__ == "__main__":
    unittest.main()
