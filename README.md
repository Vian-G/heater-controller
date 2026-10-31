# Heater controller

Runs locally on the Windows 8.1 hardware laptop using Python 3.11. GitHub stores code; the laptop controls the directly connected Phidgets.

The supplied control code is preserved, with pasted HTML/Markdown escapes removed. Heater serial **343887**, channels **0, 1, 2**; sensor serial **341989**, channels **0, 1, 2, 3**. All heaters use the four-sensor average: ON below target minus 1 C, OFF above target plus 1 C, otherwise retain their state. A spread greater than 30 C turns heaters off and stops the loop. Loop interval: 0.5 seconds; log interval: 5 seconds. Logging remains `logs/<start time>/config.json` and `data.csv` with columns `timestamp,TI_0_C,TI_1_C,TI_2_C,TI_3_C,avg_C`. Logs stay on the laptop and are ignored by Git.

## Install once on Windows 8.1

1. Keep the installed Python 3.11. If needed, install [Python 3.11.9](https://www.python.org/downloads/release/python-3119/) with pip and the Python launcher. [Python 3.11 supports Windows 8.1](https://docs.python.org/3.11/using/windows.html).
2. Install [Git for Windows 2.46.2](https://github.com/git-for-windows/git/releases/tag/v2.46.2.windows.1): choose `Git-2.46.2-64-bit.exe` for 64-bit Windows or `Git-2.46.2-32-bit.exe` for 32-bit Windows. Enable Git in Command Prompt. This older release supports Windows 8.1; newer Git releases have changed OS requirements ([requirements](https://gitforwindows.org/requirements.html)).
3. Install the matching 32-bit or 64-bit [Phidgets Windows drivers / Control Panel](https://www.phidgets.com/docs/OS_-_Windows). Phidgets documents Windows 8 and newer support. Confirm the devices are visible, then close any Control Panel channel windows before running the script.
4. Open **Command Prompt** (`cmd`). Run:

```bat
py -3.11 --version
git --version
cd /d "%USERPROFILE%"
git -c credential.helper= clone https://github.com/Vian-G/heater-controller.git
cd heater-controller
py -3.11 -m pip install -r requirements.txt
py -3.11 heater.py
```

Enter the target temperature in C when prompted. For HTTPS authentication, enter your GitHub username and use a personal access token as the password (GitHub account passwords do not work). Give the token read access to this private repository on the hardware laptop. The `credential.helper=` option avoids relying on browser sign-in helpers on the older OS and does not save the token. Never put a token in a URL or a project file.

The only Python dependency is pinned to [Phidget22 1.26.20260821](https://pypi.org/project/phidget22/), which lists Python 3.11 and supplies 32-bit and 64-bit Windows wheels. Compatibility is based on package metadata and vendor documentation; this project has **not been tested on Windows 8.1 or actual Phidgets hardware**.

## Download updates and run

Finish/stop the current controller run before updating. In Command Prompt:

```bat
cd /d "%USERPROFILE%\heater-controller"
git -c credential.helper= pull --ff-only
py -3.11 -m pip install -r requirements.txt
py -3.11 heater.py
```

Keep code edits on the modern laptop; commit and push there, then pull on the hardware laptop. If pull reports local edits or diverging history, stop and review them. Logs are retained during ordinary updates.

## Develop on the modern laptop

The GitHub repository is [Vian-G/heater-controller](https://github.com/Vian-G/heater-controller) and must remain **Private**. Clone it on the modern laptop using HTTPS:

```bat
git clone https://github.com/Vian-G/heater-controller.git
cd heater-controller
```

After editing code on the modern laptop:

```bat
git add heater.py README.md requirements.txt .gitignore
git commit -m "Describe the change"
git push
```

Then use the update commands above on the hardware laptop.
## Existing shutdown behavior

Ctrl+C exits the loop and reaches the existing `finally` block, which closes channels. The original code does not explicitly turn outputs off in `finally`, and device attachment occurs before the protected block. These behaviors were preserved; this packaging work does not establish fail-safe shutdown. Use the setup's physical heater shutdown procedure when stopping or handling errors.
