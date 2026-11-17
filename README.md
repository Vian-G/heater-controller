# Heater controller

Single-file predictive PI temperature control for the existing SRS box. Default is **hardware control**. Running `heater.py` connects to the existing Phidgets. Simulation is available only with `--simulate`. There is no initial waiting period: the first valid reading can enable heat immediately.

```bat
py -3.11 heater.py
py -3.11 -m unittest -v
py -3.11 validate_control.py --average-seconds 3 --output validation_results.json
```

Run `py -3.11 heater.py`, enter a target, and use `STOP` + Enter or Ctrl+C to stop. For hardware-free simulation, run `py -3.11 heater.py --simulate`. Independent hardware overtemperature protection and inspected mains wiring/protection remain necessary. Software cannot stop welded relay contacts or protect against total computer/communication failure. No hardware was actuated during development or validation.

Heater serial **343887**, configured channels **(0, 1, 2)**; temperature sensor serial **341989**, channels **0, 1, 2, 3**. `HEATER_CHANNELS` accepts any nonempty unique subset of 0 through 3. The fourth heater is not enabled by default. All four sensors are validated, then averaged. Maximum sensor spread remains **30 C**; maximum individual sensor temperature remains **150 C**, unchanged. Faults latch OFF, and all attached outputs are commanded OFF and devices closed on exit, including partial setup and logging failures.

## Control and relay scheduling

Each temperature channel is configured to its device-reported minimum data interval (the [1048 specification](https://www.phidgets.com/?prodid=1028) lists 32 ms). The program polls at 10 Hz and averages each thermocouple's readings over `TEMPERATURE_AVERAGE_SECONDS`, currently **1 second**. Set this constant to 3.0 for the earlier three-second window. A partial window is used at startup, so there is no added waiting period. Every raw reading is checked for sensor faults, spread and overtemperature before averaging; smoothing cannot hide a safety trip. JSON records the averaging window, polling interval and effective device intervals.

The controller updates every five seconds using the windowed averages. It predicts a short distance ahead using a filtered temperature slope, then applies PI to that predicted error. Full capacity is available immediately when far below target. Large warm-up errors do not accumulate integral; near target, negative predicted error can unload integral even at zero output. The same equation spans warm-up and regulation, with no preloaded full-power integral at handoff. There is no fixed heat-loss feedforward: the available run included an unplugged heater and cannot establish reliable high-temperature heat loss.

Settings are provisional: `KP=0.25` heater equivalents/predicted C, `KI=0.001` heater equivalents/(predicted C*s), lookahead **60 s**, slope filter **20 s**. The integral is enabled inside the larger of 10 C or N/KP. See [CONTROL_VALIDATION.md](CONTROL_VALIDATION.md) for model assumptions, comparisons, tuning and limitations. **Simulation does not establish +/-2 C regulation on the box.**

The user-confirmed relay minimum is now **30 seconds ON and 30 seconds OFF per relay**, replacing the earlier 60-second requirement. At nominal 30-second slots, the scheduler tracks requested minus delivered **heater-seconds**, choosing the next integer count to reduce that difference. It never simply rounds the controller demand or snaps small fractions to integers. Actual elapsed ON time counts even during late slots or blocked changes. No old heat debt is repaid when demand reaches zero. Normal changes honor each relay's dwell, measured after successful USB writes; emergency stop, manual stop and faults bypass dwell and command every output OFF immediately.

Runtime-based selection balances enabled heaters: start eligible heaters with less ON time, stop those with more, and rotate one pair at most once per ten minutes when a steady integer load creates sufficient imbalance. No heater is swapped solely to churn assignments at every slot. The console uses nonblocking Windows key polling for STOP, avoiding the background input reader that previously ended runs.

## Logs and validation

Logs retain `logs/<start time>/config.json` and `data.csv`, timezone-aware timestamps, five-second rows/status, and a final shutdown row. Original temperature columns now contain the configured-window channel averages; `avg_C` is their four-channel mean. `pi_output` (the predictive PI demand), four heater-state columns and runtime/transition columns remain. Appended diagnostics show control mode, filtered slope, predicted temperature, integral output and duty error. Additional `raw_TI_0_C` through `raw_TI_3_C` columns retain the latest unaveraged readings, including a fault-causing reading when available. A final fault row retains the last computed averages alongside the latest raw readings and fault reason. JSON records hardware/simulation mode and all control settings. Relay states are successfully issued commands, not electrical-current or contact feedback; an unplugged heater cannot be detected from them.

The November 16 50 C run is analyzed with **heater 0 delivering zero heat**, per the user's correction. November 14 logs were not found. [validation_results.json](validation_results.json) contains the deterministic comparison results; `validate_control.py` uses only the standard library and actual simulated relay states to heat the model. `--sweep` reproduces the small tuning comparison. Tests never actuate hardware. The saved comparison uses the earlier three-second window. The annotated 50 C historical run is tracked in Git; other logs stay local and are ignored.

The controller commands below launch hardware control. Add `--simulate` only when simulation is wanted.

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

The console prints all four sensor temperatures (`TI_0` through `TI_3`), average, target, PI output and heaters ON every **5 seconds**. The existing optional separate graph reads all four temperatures and the average from the latest CSV, with the latest 360 readings visible (about 30 minutes). It follows new runs automatically and shows the age of the last sample when readings stop. The graph also reads simulation logs; check each run's `config.json` for its mode.

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
