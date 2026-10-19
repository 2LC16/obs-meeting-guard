# OBS Meeting Guard

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6)](#)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](#)
[![Dependencies](https://img.shields.io/badge/dependencies-none-success)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[简体中文](README.md) | **English**

Always forgetting to stop your OBS recording after a meeting ends? This tiny background guard watches your **Tencent Meeting** (including the **Tencent Meeting built into WeCom / WeChat Work**) and stops the OBS recording for you — so it never runs for hours, eating tens of gigabytes of disk and capturing whatever happens on your desktop afterwards.

It calls OBS's `StopRecord` as soon as **any** of these conditions is met:

| # | Stop condition | Default |
|---|----------------|---------|
| ① | The meeting ended (meeting window gone longer than a grace period) | 30 s grace |
| ② | The screen has been static for a set duration (**and desktop audio is quiet at the same time**) | 900 s (15 min) |
| ③ | Desktop audio level (system output mix, **microphone excluded**) stayed below a threshold | -45 dB for 720 s (12 min) |

> **Gating for condition ②**: while the meeting is still producing sound (desktop audio > -45 dB), a static screen will **not** stop the recording — this avoids false stops when someone is sharing a static slide but still talking. Set `static_require_quiet = false` to disable the gate.

## Highlights

- **Covers both meeting clients**: standalone Tencent Meeting (`wemeetapp.exe`) and WeCom meetings (`WXWork.exe`; meeting window class names `Meeting*` / `Qt*` verified from the binaries);
- **GUI + console dual mode**: a native status window (status lights + progress bars + live log) by default, or plain console logging with `--console`;
- **Pure Python standard library** — zero third-party dependencies at runtime (the UI is a native Win32 window, no tkinter);
- Ships a minimal OBS WebSocket client (obs-websocket v5 protocol, with password auth, auto-reconnect and version adaptation);
- WASAPI loopback captures desktop audio (microphone not involved); ctypes screen capture detects a static picture;
- One-click self test (`--selftest` / the "Self Test" button), dry-run mode (`--dry-run`), window listing (`--list-windows`);
- **Runs fully locally** — no internet access (except the local OBS connection), no data ever leaves your machine.

## Download & Install

### Option 1: Ready-made exe (recommended)

1. Grab the latest `obs_meeting_guard.exe` (GUI) and optionally `obs_meeting_guard_console.exe` (console) from the [Releases page](../../releases), put them in any folder;
2. Double-click `obs_meeting_guard.exe` — the status window opens, **no black console**;
3. Click **Start Guard**; you can minimize the window, the guard keeps running in the background;
4. Closing the window asks for confirmation (while guarding); **closing the guard does NOT stop an OBS recording**;
5. Prefer plain logs? Double-click `obs_meeting_guard_console.exe`.

> If the first launch shows "Failed to load Python DLL": that's antivirus software (e.g. 360) racing with the one-file self-extraction. Trust/allow the file and retry — it won't happen again afterwards.

### Option 2: Run with Python

Requires Windows 10/11 + Python 3.8 or newer (**no third-party packages needed**):

```bat
python obs_meeting_guard.py            :: GUI (default)
python obs_meeting_guard.py --console  :: console mode
```

### Build the exe yourself

With Python 3.8+ installed (PyInstaller needed temporarily):

```bat
python -m pip install pyinstaller
build_exe.bat
```

## OBS setup

1. OBS menu **Tools → obs-websocket Settings** (built into OBS Studio 28+);
2. Enable the **WebSocket server**, keep the default port `4455`;
3. If you enable **authentication**: the guard automatically reads the password from OBS's own config file (`%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json`, local read only). For portable OBS setups, fill `[obs] password` in `config.ini` manually;
4. Recommended: run the self test once — `obs_meeting_guard_console.exe --selftest` (or the **Self Test** button). All four checks green means you're good to go.

## The UI

The status window has four areas:

- **Three status lights**: OBS recording (green = recording / guard active), meeting (green = meeting in progress), desktop audio (green = sound, yellow = accumulating quiet time, red = capture unavailable);
- **Three condition progress bars**: live countdown of each stop condition (counting only while recording); when desktop audio is audible, the "static screen" bar is annotated "(audio active, won't trigger)";
- **Current desktop audio level** (dB) and OBS connection status;
- **Live log** (last 300 lines; full log in `guard.log`);
- **Buttons**: Start / Stop Guard, Dry Run (log only, don't actually stop), Self Test, Config, Exit.

## How it works

The main loop runs once per second (identical logic in GUI and console mode):

1. **Meeting detection**: enumerate top-level windows of the Tencent Meeting / WeCom processes; a meeting window is judged by "class name contains `Meeting`/`Qt` + size above minimum + title not a main window" (all rules adjustable in `config.ini`);
2. **Audio capture** (separate thread): WASAPI loopback continuously captures the default playback mix, computes the RMS level (dBFS) and accumulates "below -45 dB" duration;
3. **Screen detection**: every 30 s capture the whole virtual screen and compare mean absolute difference against the previous frame; below threshold counts as "no change";
4. **OBS state**: poll every 2 s whether a recording is running;
5. **Condition evaluation**: only while OBS is actually recording; on any condition met, call `StopRecord` and reset all timers;
6. Manual recording start/stop is detected automatically; the OBS connection auto-reconnects.

Notes:

- The "meeting ended" condition only arms after a meeting has been seen **during the current recording**, so recording something else never gets falsely stopped;
- **The static-screen condition is gated by quiet desktop audio by default**: while the level is above -45 dB (someone is talking), static time still accumulates but never triggers, and the progress bar says "(audio active, won't trigger)". Set `static_require_quiet = false` to make it independent;
- Dry-run mode only logs "would stop now" without touching OBS — great for tuning thresholds;
- A heartbeat line is logged every 5 minutes so you can confirm the guard is alive.

## Configuration (config.ini)

All thresholds live in `config.ini`; save and restart to apply. The repo's [config.ini](config.ini) documents every option with the defaults.

### [obs] — OBS connection

| Key | Default | Description |
|-----|---------|-------------|
| host / port | 127.0.0.1 / 4455 | OBS WebSocket address |
| password | empty | OBS WebSocket password (auto-read can cover it) |
| auto_read_obs_password | true | Read the password from OBS's config file when left empty (local read only) |

### [guard] — stop conditions

| Key | Default | Description |
|-----|---------|-------------|
| meeting_gone_grace_sec | 30 | How long the meeting window must be gone before "meeting ended" (debounce) |
| static_check_interval_sec | 30 | Screen comparison interval |
| static_duration_sec | 900 | How long the screen must stay static before stopping |
| static_diff_threshold | 0.5 | Difference threshold (0-255 mean absolute diff); larger = easier to count as static |
| quiet_threshold_db | -45 | Quiet threshold for desktop audio (dBFS) |
| quiet_duration_sec | 720 | How long audio must stay quiet before stopping |
| static_require_quiet | true | true = static condition only triggers when audio is also below threshold; false = independent |
| static_quiet_require_meeting | false | true = static/quiet conditions only apply while a meeting is detected |
| auto_start_recording | false | true = also start recording automatically when a meeting is detected |

### [meeting] — meeting detection

| Key | Default | Description |
|-----|---------|-------------|
| process_names | wemeetapp.exe,WXWork.exe,WXWorkWeb.exe | Processes to watch |
| min_width / min_height | 500 / 350 | Minimum meeting window size (filters floating toolbars) |
| meeting_class_keywords | Meeting,Qt | Window class containing any keyword → meeting window |
| meeting_title_keywords | empty | Title containing any keyword → also a meeting window (off by default) |
| class_exclude_keywords | PerryShadow,TitleBar,IME,... | Class containing any keyword → excluded (helper/hidden windows) |
| home_titles | 企业微信,微信,腾讯会议,... | Title exactly equal → excluded (main windows) |
| title_exclude_keywords | 加入会议,设置,邀请,... | Title containing any keyword → excluded (applies to the title-keyword signal only) |

### [gui] — user interface

| Key | Default | Description |
|-----|---------|-------------|
| always_on_top | false | Keep the window on top |
| notify_on_stop | true | Balloon notification when a recording is auto-stopped |

### [log] — logging

| Key | Default | Description |
|-----|---------|-------------|
| level | INFO | Log level (DEBUG logs every window seen — great for troubleshooting) |
| file | guard.log | Log file next to the program, auto-rotating, 3 backups |

## Calibrating meeting detection (important)

Detection uses window class/title heuristics; the defaults cover the vast majority of cases. If you ever see "meeting ended but recording continued" or "thinks a meeting is on when it isn't":

1. While in a meeting, run `obs_meeting_guard_console.exe --list-windows wxwork` (or `wemeet`) and note the meeting window's **title and class name**;
2. Adjust the `[meeting]` section of `config.ini`:
   - class name lacks `Meeting`/`Qt` → add the actual class keyword to `meeting_class_keywords`;
   - or fill `meeting_title_keywords` to enable title matching;
3. Optionally set `log.level = DEBUG` — the log then lists every window seen at each check.

## FAQ

**Q: "Failed to load Python DLL" on first launch?**
Antivirus real-time scanning raced with the one-file self-extraction. Allow/trust the exe and retry; it won't recur.

**Q: The progress bars don't move?**
They only count while OBS is recording. Also the "meeting ended" bar only arms after a meeting was seen during the current recording.

**Q: Meeting ended but recording wasn't stopped?**
Check `guard.log` meeting-detection lines: if it kept saying "no meeting window", the window class/title isn't in the default rules — see [calibration](#calibrating-meeting-detection-important). If it said "meeting in progress", some other large window was misdetected.

**Q: Recording stopped too early?**
- Long static slide share with nobody talking (desktop audio quiet too) → the static condition fired; raise `static_duration_sec`. As long as there is sound, static never triggers;
- Everyone muted → the quiet condition fired; lower `quiet_threshold_db` (e.g. -60) or raise `quiet_duration_sec`;
- Want the "meeting ended" condition only → set `static_duration_sec` and `quiet_duration_sec` to 86400.

**Q: Desktop audio capture unavailable?**
The self test tells you why (typical on machines without a playback device). The quiet condition is disabled then; the others still work.

**Q: I minimize the meeting window during calls — does it still count?**
A minimized meeting window still counts as "in a meeting". If desktop audio is also quiet, the static condition can still stop the recording after 15 minutes; raise `static_duration_sec` if undesired.

**Q: Does the meeting's elapsed-time timer break static detection?**
Somewhat: the ticking digits count as "change". But detection compares the **whole-screen mean absolute difference** (default 0.5/255); a small timer usually stays below it. If meetings never register as static (log shows frequent "screen active again"), raise `static_diff_threshold` to ~1.0–2.0. Real meeting endings are caught by condition ① anyway.

**Q: Autostart on boot?**
Put a shortcut to `obs_meeting_guard.exe` into `shell:startup` (type `shell:startup` in the Run dialog).

**Q: Does exiting the guard stop the recording?**
No. Exiting only stops watching; OBS keeps recording.

## Running the tests (optional)

```bat
cd tests
run_all.bat
```

Covers: static-screen detection (8 checks), audio capture, OBS protocol (13), end-to-end guard logic (10, including the "static but audio active" gate), GUI (23), and GUI-exe end-to-end (6) — 61 assertions in total.

## Repository layout

```
obs-meeting-guard/
├── obs_meeting_guard.py           # single-file source (commented, stdlib only)
├── config.ini                     # all thresholds live here
├── build_exe.bat                  # rebuild both exes (pip install pyinstaller)
├── start_guard.bat                # double-click to start the GUI version
├── requirements.txt               # dependency note (zero at runtime)
├── README.md / README.en.md       # Chinese / English docs
├── LICENSE                        # MIT
└── tests/                         # optional test scripts
```

## Privacy

The program **runs entirely on your machine**: no internet access, no telemetry, no registry writes, no services. The only network connection is the local OBS WebSocket (127.0.0.1:4455); the only extra file read is OBS's own config for the WebSocket password (optional).

## Uninstall

Delete the folder. No registry entries, no services, no leftover processes.

## License

[MIT](LICENSE)
