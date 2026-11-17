# Heater controller

Runs locally on the Windows 8.1 hardware laptop using Python 3.11. GitHub stores code; the laptop controls the directly connected Phidgets.

`heater.py` is a single-file PI controller with no GUI. **Running it connects directly to the existing Phidgets and controls the heaters.** Enter a target in C, and type `STOP` + Enter or press Ctrl+C to stop. There is no dry-run flag or simulation mode; tests use mocked hardware.

Hardware remains heater serial **343887**, enabled channels **0, 1, 2**, and sensor serial **341989**, channels **0, 1, 2, 3**. Edit only `HEATER_CHANNELS` to select any nonempty subset of channels 0 through 3. Supporting four heaters does not enable the fourth by default.

Operation requires inspected mains wiring and electrical protection and an **independent hardware overtemperature cutoff**. Review `MAX_SAFETY_TEMPERATURE_C` (initially 150 C) for the actual box, materials and process. Software OFF commands cannot protect against welded relay contacts, USB loss, or total computer failure. Actual hardware operation has not been validated here.

## Control and relay scheduling

The PI uses the average of all four thermocouples every 5 seconds and produces 0 to N heater equivalents. `KP = 0.10` has units heater equivalents/C; `KI = 0.0005` has units heater equivalents/(C*s); derivative gain is zero. The bounded integral freezes when error drives further into saturation and can unwind when error reverses. Integration and relay timing use monotonic elapsed time. Gains are provisional: November 14 three-element test data is not included, and these values do **not** guarantee +/-2 C control.

At nominal 60-second slots, a fractional accumulator distributes demand across adjacent integer counts: 2.5 alternates 2 and 3, while 3.7 uses 4 heaters in seven out of ten slots. Demands within 0.01 of an integer use that integer to avoid tiny dithering. Each relay separately enforces at least 60 seconds ON and OFF, including an initial 60-second OFF dwell. Delayed slots are not replayed. Actual changes can be later because of dwell, polling or communication time; dwell takes priority over matching demand.

Additional heat selects eligible heaters with the least cumulative ON time; reducing heat turns off those with the most. Steady partial integer loads rotate one pair when runtime imbalance reaches 600 seconds, at most once per 600 seconds. This distributes use across all enabled heaters without continuous swapping. Full demand necessarily keeps all enabled heaters ON.

Sensors are checked every 0.25 seconds independently of relay slots. Missing, invalid or nonfinite readings, spread above 30 C, any sensor above the safety limit, read/write exceptions, detach events and Phidgets error events latch a shutdown. All Phidgets error events are conservatively treated as faults; see the vendor's [error-event guidance](https://www.phidgets.com/docs/Handling_Errors_and_Logging). STOP, Ctrl+C and shutdown also send OFF immediately without respecting dwell. OFF is attempted for every configured output even if one write fails, and all created device handles are closed in `finally`, including partial setup. An OFF failure is logged and cannot establish the physical contact state. A fault requires fixing the cause and manually starting a new run; reconnection never resumes a stopped run.

## Logs and tests

Logging remains `logs/<start time>/config.json` and `data.csv`, with timezone-aware timestamps. The original first six columns remain `timestamp,TI_0_C,TI_1_C,TI_2_C,TI_3_C,avg_C`. Additional columns record target, PI demand, count ON, four commanded heater states, enabled heaters' cumulative ON seconds, transitions, last monotonic transition times, controller state and fault reason. Disabled heaters log zero state. State records commands, not physical contact feedback. Rows/status print every 5 seconds, plus a final shutdown/fault row. JSON records configuration, units and `mode: "hardware"`. Logs stay local and are ignored by Git.

Run the deterministic, hardware-free standard-library tests with:

```bat
py -3.11 -m unittest -v
```

Tests cover all 15 nonempty channel subsets, integer/fractional demand, balanced runtime, dwell, delayed slots and USB writes, rapid target changes, anti-windup, emergency/STOP/Ctrl+C shutdown, faults while heating, fault latching, partial setup cleanup and CSV/JSON structure. No real Phidgets are imported or actuated.

## Install once on Windows 8.1

1. Keep the installed Python 3.11. If needed, install [Python 3.11.9](https://www.python.org/downloads/release/python-3119/) with pip and the Python launcher. [Python 3.11 supports Windows 8.1](https://docs.python.org/3.11/using/windows.html).
2. Install [Git for Windows 2.46.2](https://github.com/git-for-windows/git/releases/tag/v2.46.2.windows.1): choose `Git-2.46.2-64-bit.exe` for 64-bit Windows or `Git-2.46.2-32-bit.exe` for 32-bit Windows. Enable Git in Command Prompt. This older release supports Windows 8.1; newer Git releases have changed OS requirements ([requirements](https://gitforwindows.org/requirements.html)).
3. Install the matching 32-bit or 64-bit [Phidgets Windows drivers / Control Panel](https://www.phidgets.com/docs/OS_-_Windows). Phidgets documents Windows 8 and newer support. Confirm the devices are visible, then close any Control Panel channel windows before running the script.
4. With Python 3.11, Git, and the Phidgets drivers installed, open **Command Prompt** (`cmd`) and paste this single line to download the project, install its Python dependency, and launch it:

```bat
cd /d "%USERPROFILE%" && git clone https://github.com/Vian-G/heater-controller.git && cd heater-controller && py -3.11 -m pip install -r requirements.txt && py -3.11 heater.py
```

Use this setup command only for the first download; if `%USERPROFILE%\heater-controller` already exists, use the update command below. Each `&&` runs the next step only if the preceding step succeeds. Enter the target temperature in C when prompted. The repository is public, so cloning and pulling do not require a GitHub login or token.

The only Python dependency is pinned to [Phidget22 1.26.20260821](https://pypi.org/project/phidget22/), which lists Python 3.11 and supplies 32-bit and 64-bit Windows wheels. Compatibility is based on package metadata and vendor documentation; this project has **not been tested on Windows 8.1 or actual Phidgets hardware**.

## Download updates and run

Finish/stop the current controller run before updating. In Command Prompt:

```bat
cd /d "%USERPROFILE%\heater-controller" && git pull --ff-only && py -3.11 -m pip install -r requirements.txt && py -3.11 heater.py
```

Keep code edits on the modern laptop; commit and push there, then pull on the hardware laptop. If pull reports local edits or diverging history, stop and review them. Logs are retained during ordinary updates.

## Live temperature graph

The console prints all four sensor temperatures (`TI_0` through `TI_3`), average, target, PI output and heaters ON every **5 seconds**. The existing optional separate graph reads all four temperatures and the average from the latest CSV, with the latest 360 readings visible (about 30 minutes). It follows new runs automatically and shows the age of the last sample when readings stop. Older simulation logs may still be present; check their `config.json` for run mode.

After downloading updates, open another **Command Prompt** and run:

```bat
cd /d "%USERPROFILE%\heater-controller" && py -3.11 graph.py
```

Or launch the graph in a separate window and the controller from one Command Prompt command:

```bat
cd /d "%USERPROFILE%\heater-controller" && start "Temperature graph" py -3.11 graph.py && py -3.11 heater.py
```

The graph uses Python's bundled [tkinter](https://docs.python.org/3.11/library/tkinter.html); no additional pip package is needed. If tkinter is missing, modify the Python 3.11 installation and enable **tcl/tk and IDLE**. The graph only reads log files and never connects to Phidgets. Closing it does not stop the controller. Before the next run creates a log, the graph may show the previous run, identified by its run folder and last sample time. CSV parsing and graph calculations were checked without a display; an actual graph window and hardware operation have not been tested.

## Develop on the modern laptop

The GitHub repository is [Vian-G/heater-controller](https://github.com/Vian-G/heater-controller). Clone it on the modern laptop using HTTPS:

```bat
git clone https://github.com/Vian-G/heater-controller.git
cd heater-controller
```

After editing code on the modern laptop:

```bat
git add heater.py graph.py README.md requirements.txt .gitignore
git commit -m "Describe the change"
git push
```

Pushing changes requires GitHub authentication on the modern laptop. Then use the update command above on the hardware laptop.
