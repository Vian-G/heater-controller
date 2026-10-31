import csv
import json
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from Phidget22.Devices.DigitalOutput import DigitalOutput
from Phidget22.Devices.TemperatureSensor import TemperatureSensor

HEATER_SERIAL = 343887
SENSOR_SERIAL = 341989
HEATER_CHANNELS = (0, 1, 2)
SENSOR_CHANNELS = (0, 1, 2, 3)
BAND_C = 1  # Degrees C either side of target.
MAX_SENSOR_SPREAD_C = 30  # Stop if hottest minus coldest exceeds this.
LOG_INTERVAL_SECONDS = 5
LOOP_INTERVAL_SECONDS = 0.5
ATTACH_TIMEOUT_MS = 5000
LOG_DIR = Path(__file__).parent / "logs"


def main():
    target = float(input("Target temperature (C): "))
    started = datetime.now().astimezone()
    folder = LOG_DIR / started.strftime("%Y-%m-%d_%H-%M-%S_%f")
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({
        "started_at": started.isoformat(),
        "target_C": target,
        "band_C": BAND_C,
        "max_sensor_spread_C": MAX_SENSOR_SPREAD_C,
        "heater_serial": HEATER_SERIAL,
        "heater_channels": HEATER_CHANNELS,
        "sensor_serial": SENSOR_SERIAL,
        "sensor_channels": SENSOR_CHANNELS,
        "log_interval_seconds": LOG_INTERVAL_SECONDS,
    }, indent=2))
    print(f"Logging to {folder}")
    heaters = []
    sensors = []

    for i in HEATER_CHANNELS:
        h = DigitalOutput()
        h.setDeviceSerialNumber(HEATER_SERIAL)
        h.setChannel(i)
        h.openWaitForAttachment(ATTACH_TIMEOUT_MS)
        h.setState(False)
        heaters.append(h)

    for i in SENSOR_CHANNELS:
        s = TemperatureSensor()
        s.setDeviceSerialNumber(SENSOR_SERIAL)
        s.setChannel(i)
        s.openWaitForAttachment(ATTACH_TIMEOUT_MS)
        sensors.append(s)

    on = False
    try:
        with (folder / "data.csv").open("w", newline="") as log:
            writer = csv.writer(log)
            writer.writerow(["timestamp", *[f"TI_{i}_C" for i in SENSOR_CHANNELS], "avg_C"])
            next_log = monotonic()
            while True:
                temps = [s.getTemperature() for s in sensors]
                avg = sum(temps) / len(temps)
                spread = max(temps) - min(temps)
                if spread > MAX_SENSOR_SPREAD_C:
                    for h in heaters:
                        h.setState(False)
                    print(f"STOP: Sensor difference {spread:.2f} C exceeds {MAX_SENSOR_SPREAD_C} C.")
                    writer.writerow([datetime.now().astimezone().isoformat(), *temps, avg])
                    log.flush()
                    break
                if avg < target - BAND_C:
                    on = True
                elif avg > target + BAND_C:
                    on = False
                for h in heaters:
                    h.setState(on)
                print(" | ".join(f"TI_{i}: {temp:.2f} C" for i, temp in zip(SENSOR_CHANNELS, temps))
                      + f" | Avg: {avg:.2f} C | heaters {'ON' if on else 'OFF'}")
                if monotonic() >= next_log:
                    writer.writerow([datetime.now().astimezone().isoformat(), *temps, avg])
                    log.flush()
                    next_log += LOG_INTERVAL_SECONDS
                sleep(LOOP_INTERVAL_SECONDS)
    finally:
        for h in heaters:
            h.close()
        for s in sensors:
            s.close()


if __name__ == "__main__":
    main()
