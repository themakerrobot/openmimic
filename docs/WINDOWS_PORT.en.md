# Windows native port report

<a href="WINDOWS_PORT.md">한국어</a> · <b>English</b>

What was changed so that one code base runs on Ubuntu 22.04 / 24.04 / 26.04 and Windows 10 / 11, and what was and was not verified.

> After the port, the virtual device feature (virtual arms · virtual cameras · MuJoCo physics simulation — `tools_simarms.py`, `openmimic_fakeserial.py`,
> `openmimic_simcam.py`, `openmimic_mujoco.py`) was removed. Items below that deal with the virtual device are a record of the time of the port.

**Summary**

- Ubuntu passed the regression tests on every virtual-device configuration. The same tests were run on the code before the change and the results compared.
- Windows is **officially supported natively (without WSL), on equal terms with Ubuntu** — single and bimanual, the same code from setup to rollout.
  On a Lunar Lake laptop (Core Ultra 200V · Arc 140V · 32 GB RAM) · `win-intel` + XPU · bimanual SO-ARM101 · 3 USB cameras, it was confirmed from
  install → start (`OpenMimic.bat`) → setup wizard (ports · diagnosis · calibration · verify) → control → collect → XPU training → OpenVINO conversion → NPU rollout.
  The grasp itself has not succeeded yet, which is a device limit rather than a feature problem — XPU training takes about 2.3 s per step on the integrated GPU,
  so training stopped at 10 episodes · 5000 steps (3 h 15 min), short of the data and training needed for grasping (about 50 demos · tens of thousands of steps).
  The recommended setup is to do full training on an NVIDIA GPU or HF Jobs and only run the model on this device's NPU. Problems found on real hardware and their fixes are in section 3-1.
- Not yet seen on real hardware: Windows + NVIDIA (`win-cuda`), OMX, single-arm setup, camera and board identification after a reboot, autostart, job stop signals — see the table in section 6 and the list in section 4.
  Single-arm uses the same code path as bimanual (with one arm), so there is no separate OS handling for it.
- Windows behaviour we are not sure about is marked `TODO(win-verify)` in the code (list below).

## 1. Changed files and why

| File | What · why |
|---|---|
| `openmimic_platform.py` (new) | Collects everything that differs by OS in one place: listing ports and cameras and resolving their identifiers, starting · stopping · checking jobs, shared folders, MuJoCo render backend, per-OS messages (`TEXT`), platform name. The Linux implementation is the existing code moved as is |
| `openmimic_fakeserial.py` (new) | Virtual arm port `sim://<name>`. A factory sits in place of `serial.Serial` and creates a `FakeSerial` (127.0.0.1 socket) only for `sim://`; real ports use the original pyserial. Replaces the PTY so Linux and Windows take the same path |
| `tools_simarms.py` | Removed PTY · symbolic links · `select(stdin)` and serves the boards through `SimPortServer`. Added CTRL_BREAK to the stop handler. Also fixed holding the board lock and the world lock at the same time (section 3) |
| `main.py` | Changed starting · stopping · checking jobs, listing · comparing · resolving ports and cameras, port and camera format checks and messages, the collect worker stop handler, reading command files (`take_file`), `PYTHONUTF8=1` in job environments, and the platform display on the setup screen. It no longer looks at `os.name` · `sys.platform` directly |
| `openmimic_rollout.py` | Forwards CTRL_BREAK to lerobot's SIGINT handler — lerobot only handles SIGINT · SIGTERM, so on Windows "stop" ended without cleanup. A servo-fault stop uses `signal.raise_signal` instead of `os.kill(getpid, SIGINT)`. Also installs the virtual serial |
| `tools_armcheck.py` · `tools_dxlcheck.py` | Port listing · resolving and stdin Enter detection moved to platform functions. `sim://` ports can be used from the CLI |
| `openmimic_mujoco.py` · `openmimic_simcam.py` | Render frame folder through `frame_dir()` (Windows: `%TEMP%\openmimic_sim`), render backend choice through `setup_mujoco_gl()` (Windows: `wgl`). Frame files of the same size are overwritten (Windows cannot truncate a file that is mapped) |
| `openmimic_ov.py` | NPU · GPU driver messages split by OS |
| `install.ps1` · `openmimic.ps1` · `activate.ps1` (new) | Windows versions of `lerobot_conda.sh` · `openmimic.sh` · `activate.sh`. Saved with a UTF-8 BOM (Windows PowerShell 5.1 reads files without a BOM in the system code page) |
| `.gitattributes` (new) | Check out `*.ps1` with CRLF and `*.sh` with LF |
| `openmimic_i18n_en.json` | English for the Windows messages and new screen text |
| `README.md` · `README.en.md` · `docs/INSTALL.md` · `docs/INTEL.md` · `docs/DEVELOPMENT.md` | Windows quick start, install · file locations · `openmimic.ps1` · platform table, troubleshooting (COM drivers · cameras), design notes |

No formats changed — calibration json, datasets (lerobot v3), checkpoints, `openmimic_config.json`, environment zip.
Port values are saved with the existing rule (by-id first, by-path otherwise). On Windows, by-id is `usb-<vid>_<pid>_<serial>` and by-path is the USB location (`1-3.2`).
Variable names such as `COM5` are not saved.

## 2. Deviations from the original plan (and why)

| Plan | Actual | Why |
|---|---|---|
| Attach the board inside FakeSerial and answer with function calls | The board lives in one virtual-arm tool process; FakeSerial connects to it over a 127.0.0.1 socket | The server · collect worker · rollout · check tools must see **the same arm**. For example the worker reads calibration registers written by the server, and there is only one MuJoCo scene and leader demo. A board per process would split the state. Sockets need no driver (pip only) and Linux · Windows take the same path (no second path to maintain) |
| Replace `serial.Serial` in workers only when `OPENMIMIC_SIM=1` | Always installed when an OpenMimic process that opens serial ports starts (server · collect worker · rollout · check tools). One replacement function, `openmimic_fakeserial.install()` | The server itself opens virtual ports (control · calibration · wizard). Gating with an environment variable would need a server restart every time the virtual device is turned on. Ports other than `sim://` get the original pyserial object, so real arms are unaffected |
| `resolve_serial_port` on Linux = `os.path.realpath`, exception if missing | Linux returns the given string as is, no exception. Same-device comparison uses `port_key` (= realpath) | Before the change, Linux passed the by-id path to lerobot as is (pyserial follows the link). The behaviour was kept so that error messages and failure hints for a missing port stay the same. Windows raises `PortNotFound` as planned |
| usb info `{vid, pid, sn, …}` | Key name kept as `serial` | The screen JS and wizard choose by-id / by-path from `usb.serial`. Renaming it would change Linux screen behaviour |
| Camera stable identifier by_path | On Windows by_id = by_path = `dshow:<device path>` | The DirectShow device path is serial-based when there is a serial, otherwise a value Windows builds from the USB location. The same value is saved whichever rule picks it |
| Autostart with Task Scheduler (`schtasks /SC ONLOGON`) | A Startup-folder shortcut (`.lnk`) | A logon-trigger task needs administrator rights, and its defaults (start only on AC power, low priority) mean it does not start on laptop battery and collection · training slow down. A shortcut needs no rights and works with non-ASCII paths |

Linux dependencies that were not in the plan's table were changed too.

- `os.kill(pid, 0)` — on Windows this does not check but **terminates the process** (2 places checking the virtual device state).
- `os.waitpid(…, WNOHANG)`, `/proc/<pid>/stat` — job liveness checks and PID reuse detection.
- `select.select([sys.stdin])` — `tools_armcheck`, `tools_simarms`.
- `os.symlink` — virtual arm port links.
- Command files "read → delete" — Windows cannot delete a file the writer has open, so the same command ran again.
- Python's default encoding — settings · logs · stdout containing Korean broke or stopped when written in the Windows default code page. Fixed with `PYTHONUTF8=1` · `-X utf8`.
- Screen messages that only pointed at Linux commands — dialout, `/dev/serial/by-id`, `lerobot_conda.sh`, `/dev/accel`.

## 3. Existing problem fixed along the way

**Problem**: automatic recording on the physics-simulation virtual device broke between episodes with `There is no status packet`.
It **reproduced at the same place with the code before the change (PTY)**.

**Cause**:

- When updating the follower, the virtual-arm tool waited for the MuJoCo world lock while holding the board lock.
- Meanwhile cube placement and demo planning (about 80 ms on this server) held the world lock.
- So the serial reply exceeded lerobot's timeout (about 35 ms).

**Fix**: inside the board lock only the registers are read; MuJoCo calls happen outside it.
After the fix, automatic recording of 3 episodes passes to the end.

**Remaining**: in a CPU-render (OSMesa) test environment, rendering holds the Python GIL and the reply thread is occasionally delayed by 27–300 ms.
In 1 of 3 MuJoCo automatic-recording runs (the first run right after starting the server) the same error happened once. GPU rendering (EGL · wgl) should suffer less because rendering is shorter, but this was not verified.

## 3-1. Problems found on real Windows hardware

**Saving settings or switching environments fails or stops halfway.**

- Cause: settings files are written to a temporary file and swapped in with `os.replace`. On Windows that fails with `PermissionError`
  if someone else has the target open at that moment — the screen refresh reading it, or Windows Defender scanning it (on Linux the swap works even while the file is open).
- Effect: when saving fails, the port assignment is not overwritten. Restoring the environment when turning off the virtual device could also stop halfway,
  leaving the environment set for virtual arms (`sim-…`) after the virtual device was off.
- Fix: `P.replace_file` retries briefly for up to 2 seconds on Windows.
  Reads also retry briefly through `P.read_text` — treating a failed read as "no file" and saving defaults would wipe the settings.
  Every place that used `os.replace` was changed (settings · environments · calibration files, collect · rollout state and previews, virtual device state, OpenVINO metadata, power records).

**Training dies right after saving the first checkpoint (`WinError 1314`).**

- Cause: lerobot creates `checkpoints/last` as a symbolic link every time it saves a checkpoint. Windows cannot create symbolic links without administrator rights or developer mode.
  The checkpoint (`005000` etc.) is already written and stays, but training longer than `save_freq` stops there and "continue training" does not work either.
- Fix: on Windows, training is started through `openmimic_train.py` (`P.LEROBOT_TRAIN`). It swaps only lerobot's `update_last_checkpoint`
  for `P.link_dir` and runs `lerobot_train` as is — a symbolic link if possible, otherwise a directory junction, which needs no rights.
  Models trained with the older version that have no `last` get linked to their last checkpoint when the model list is shown.
  `os.walk(followlinks=False)` descends into junctions, so size calculation and export skip them with `P.is_link`.

**Saving one episode takes more than a minute (3 cameras).**

- Cause: every save, lerobot encodes the videos with a `ProcessPoolExecutor`, one worker per camera. Windows has no fork (spawn),
  so each worker is a new Python that imports `main.py` · torch · lerobot from scratch — paying that cost again for every episode.
- Fix: in the collect worker, `P.avoid_spawn_pools()` replaces `ProcessPoolExecutor` with `ThreadPoolExecutor` (Windows only).
  Encoding and PNG reading release the GIL, so threads still run in parallel. Test (Linux, 3 cameras · 300 frames): spawn 11.7 s → threads 5.4 s, same number of video frames.
- The save time is shown in the "Last" field of the collect screen and in the job log. On real Windows hardware, a save of over a minute dropped to about 36 seconds.
- The time to re-read the PNGs and compress them all at once still remained, so the collect screen got **Compress video while recording** (lerobot `streaming_encoding`),
  on by default on Windows — no PNGs are written, so saving finishes almost immediately. Episodes where the encoder could not keep up and frames were dropped are discarded instead of saved.

## 3-2. Fixes from a full review (Windows)

The whole code base was re-read with Windows in mind and only places that are real problems were fixed.

**COM ports — Windows allows only one opener** (on Linux several can open the same port)

- Connecting the Control tab now goes through the same gate as other start requests (port check · calibration · verify etc.), so the Control tab does not open a port while one of those is opening it.
- Starting a rollout re-checks "in use" after the power check, the same as starting collection.
- Fixed a leader port left open with no owner when the Control tab closed while leader follow was turning on. Connect and disconnect are tied by a lock, and "in use" now includes the leader port.
- The "done" state at the end of calibration · motor ID setup · verify is set **after** the port is closed.
- The setup "full scan" (all baud rates) closes the port even on error. lerobot's `scan_port` does not close it.
- Turning off the virtual device is blocked while the Control tab · calibration · port watch etc. are using it.

**Stopping jobs**

- "Stop" (CTRL_BREAK) is sent by a console-less helper that attaches to the job's console.
  After `openmimic.ps1 restart` · `update` the server and the job are on different consoles, so sending it directly does not reach the job.
- The output of force stop (`taskkill`) is read in the system code page. Reading it as UTF-8 raised an error on Korean Windows and the stop failed.
- Cloud training jobs run as a child instead of `os.execv`. On Windows `execv` changes the pid, so OpenMimic thought the job had ended.

**Files · names**

- Renaming or deleting a dataset: if a video is open, the whole operation fails with a message.
  Before, `shutil.move` fell back to copying and split the dataset in two, and deleting removed only part of it.
- Names Windows cannot use are blocked: a trailing `.`, reserved names such as `con` · `nul` · `com1`.
- Keys of the rollout success records now always use `/`. They were saved with `\` so the success rate did not show on the model screen; old keys are converted when read.
- Calibration export (`os.uname` → `platform.node`), renaming in the repair tool (`os.replace`), finding the access address (`hostname -I` is Linux only).
- Cameras that only show up by number (no pygrabber etc.) can be saved.
- On the English screen, paths and device names are not translated. If the user folder name contained Korean, the dictionary replaced those characters and broke the path.
- A hand-typed `com5` is treated as the same port as `COM5`. The COM list is shown in numeric order.
- Turning off the virtual device clears the render frame header, so virtual cameras do not freeze on an old picture.
- Tools run directly in a terminal (`tools_armcheck` · `tools_dxlcheck` · `tools_act2bench`) write output and files in UTF-8.

**Install · launch scripts**

- `install.ps1`
  - Blocks the user site-packages and `PYTHONPATH` of other Python installs (`PYTHONNOUSERSITE`).
  - Stops the install if the environment's Python is running — `.dll` files in use cannot be replaced and the install breaks with WinError 5.
  - Uses `conda create -p … --override-channels -c conda-forge` so it does not follow existing conda settings.
- `openmimic.ps1`
  - Starts the server with `-u` so the startup message appears in the log immediately (`openmimic.sh` too).
  - Sets the server to normal priority.
  - Reports git errors separately and reports a failed stop.
  - Autostart uses a shortcut (section 2).

## 4. TODO(win-verify) list

| Location | What to check |
|---|---|
| `openmimic_platform.py` `_win_break` | Whether the console-less helper attaches to the job console and delivers CTRL_BREAK (especially "stop" after `openmimic.ps1 restart`) |
| `openmimic.ps1` background | On Windows 11 with Windows Terminal as the default terminal, whether the hidden cmd window turns into a visible one (closing a visible window stops the server and jobs too) |
| `openmimic_platform.py` `_win_port_entry` | Whether pyserial fills in the serial number · USB location for the SO-ARM101 board (CH343 `1a86:55d3`) |
| `openmimic_platform.py` `_win_cameras` | Whether the DirectShow listing order matches the OpenCV `CAP_DSHOW` index, and whether device paths stay the same after a reboot (two cameras of the same model) |
| `openmimic_platform.py` `_dshow_from_ffmpeg` | Whether indexes shift when a device is missing from the ffmpeg list (a "(none)" virtual camera etc.) |
| `openmimic_platform.py` `_win_cameras` (fallback) | With neither pygrabber nor ffmpeg, cameras are chosen by number only — the order may change after a reboot or replug |
| `openmimic_platform.py` `stdin_line_ready` | Enter detection with msvcrt in a Windows console (`tools_armcheck --sweep`) |
| `openmimic_platform.py` `replace_file` · `read_text` | Whether 2 seconds of retrying is enough (if a Defender scan takes longer, the save still fails) |
| `install.ps1` user folder | Whether miniforge · pip work in a user folder with non-ASCII characters such as Korean |
| `openmimic.ps1` autostart | Whether the Startup-folder shortcut starts it at login |

## 5. Step 5 — lerobot Windows compatibility results

| Item | Result | Basis |
|---|---|---|
| Video encoding · decoding | **Works (PyAV by design)** | The install scripts remove `torchcodec` on both OSes. lerobot `e40b58a` switches decoding to PyAV when torchcodec is missing (the `'torchcodec' is not available … falling back to 'pyav'` warning is expected), and encoding already uses PyAV. `Using video codec: libsvtav1` was seen in a collect log on real Windows hardware |
| lerobot `find_cameras()` | **Bypassed** | On Windows it opens indexes 0…N one by one with the default backend, which is slow and gives no stable identifier. OpenMimic uses the DirectShow listing (pygrabber → ffmpeg) first and falls back to `find_cameras()` only when both fail. When opening, the index + `CAP_DSHOW` is passed as `backend` in the lerobot camera config |
| OpenVINO NPU (`openmimic_ov.py`) | **Works** | On real Windows hardware OpenVINO 2026.4 found `['CPU', 'GPU', 'NPU']`, and FP16 · INT8 conversion and NPU rollout (bimanual) worked. The conversion and inference code has no OS dependency (it only uses paths and device names) |
| `OPENMIMIC_TORCH=xpu` on Windows | **Works** | Re-running with `xpu` after a CPU-wheel install removed the CPU wheel and installed `torch 2.11.0+xpu`; `torch.xpu.is_available()` is True and the device is `Intel(R) Arc(TM) 140V GPU`. The XPU wheel survived the lerobot install, and ACT training ran 5000 steps to the end (about 2.28 s per step) |
| Windows paths in the rollout CLI | **Works** (checked with draccus on Linux) | Camera paths with backslashes and spaces such as `C:\Users\…\openmimic_sim\cam_top` parse as is inside dict arguments without quotes |

## 6. Verification checklist results

### Ubuntu (regression)

The test environment was an Ubuntu 24.04 container (4 CPU cores, no GPU, no real hardware).
lerobot `e40b58a`, torch 2.11 and mujoco 3.17 were used, with MuJoCo rendering on OSMesa.

| # | Item | Result |
|---|---|---|
| 1 | Fresh `lerobot_conda.sh` install → `openmimic.sh start` → wizard → real SO-ARM101 | **Not done** (no real hardware or fresh machine). `lerobot_conda.sh` · `openmimic.sh` were not changed |
| 2 | Real-hardware collection of 5 episodes → ACT 100 steps → rollout | **Not on real hardware**. Replaced with the virtual arm (item 3). ACT takes about 14 s per step on CPU, so it was cut to 20 steps |
| 3 | Virtual single SO-ARM101 + MuJoCo: wizard → automatic recording 3 → training → rollout with automatic verdict → simulated fault with automatic stop | **Passed**. Details below |
| 4 | Virtual bimanual OMX: write factory values → verify → control | **Passed** (12/12) |
| 5 | Job stop (SIGINT) · force stop (SIGKILL), torque off after stop | **Passed**. After stopping both collection and rollout, Torque_Enable is 0 on all 6 follower motors. Force stop confirmed the worker exited |

Details for item 3:

- Wizard: port list, probe, diagnosis, two range calibrations, verify, control connect — all passed.
- Automatic recording finished 3 episodes. The last demo succeeded at the task.
- A simulated overheat (75 °C) stopped collection automatically.
- ACT 20-step training → rollout: the cube was placed again at the start, and at the end a **failure was recorded automatically** (`auto: sim`).
  Success was not expected from a 20-step model, and the success-record path was not exercised again this time.
- Port-finding watch: only the swept leader showed a large travel.

Additional configurations:

| Configuration | Result |
|---|---|
| Virtual single SO-ARM101 (no physics, manual recording) | 23/23 |
| Virtual bimanual SO-ARM101 | 27/27 |
| Virtual single OMX | 21/21 |
| `tools_jscheck.py` Korean/English | 0 JS syntax errors |
| Platform display on the setup screen | Correct in Korean/English |

The code before the change (same test script) scored 22/23 in the MuJoCo scenario; "collection finished" failed because of the existing problem in section 3.
After steps 1 · 2 it was **the same** 22/23, and 23/23 from step 3 on.

### Windows

| # | Item | Result |
|---|---|---|
| 1 | `install.ps1` → `openmimic.ps1 start` → browser | **Passed** (real Windows hardware, `win-intel`, through to `=== 완료 ===` (done); reinstalling with `xpu` on the same machine also passed. Started with `OpenMimic.bat` · `openmimic.ps1 restart` · `update` and accessed from a browser on another device). Fixed from the real install logs: on reinstall, setuptools was upgraded to the latest and printed a requirement conflict with lerobot · torch (pinned to `setuptools<81` — the Linux version too), and an empty conda stderr line was printed as `System.Management.Automation.RemoteException` |
| 2 | Real SO-ARM101: find port (COM + SN) · diagnosis · calibration · verify · control · collect | **Passed** (bimanual). Fixed on real hardware: a single late reply to an EEPROM write stopped the whole calibration save (now retried per register), a state where only one of two arms had torque on was not visible on screen, the "cannot follow" warning kept appearing from momentary error, and saving an episode took over a minute (section 3-1) |
| 3 | Register cameras → reboot → same devices | **Registration and collection passed** (DirectShow paths, 3 cameras, MJPG). Whether the same devices are found after a reboot is not yet checked |
| 4 | Virtual device + MuJoCo (wgl) | **Not done**. The virtual serial (socket) uses no OS feature and is the same code as on Linux. `wgl` rendering not verified |
| 5 | Import a dataset · calibration zip · model from Ubuntu into Windows and train · run | **Not done**. Formats were not changed |
| 6 | Core Ultra: XPU training → OpenVINO conversion → NPU rollout | **Passed** (ACT 5000 steps, about 2.3 s per step → OpenVINO conversion → bimanual rollout on the NPU; the `checkpoints/last` link permission error at the end of training is fixed in section 3-1). The grasp did not succeed — not enough training because the integrated GPU only allowed 10 episodes · 5000 steps (not a feature problem) |
| 7 | Job stop · force stop | **Not done**. The code uses CTRL_BREAK (normal) / `taskkill /T /F` (force); handlers cover SIGINT · SIGTERM · SIGBREAK |

Real Windows hardware testing should cover the 7 items above together with the list in section 4.
