# OpenMimic user guide

<a href="GUIDE.md">한국어</a> · <b>English</b>

For installation see [INSTALL.md](INSTALL.md) (Korean); for the overview see the [README](../README.en.md). This guide covers each screen and troubleshooting.
The screen switches between Korean and English with the **한/EN** button in the top bar. The screenshots show the Korean screen.

- [First setup — setup wizard](#first-setup--setup-wizard)
- [Using each screen](#using-each-screen)
- [Several arm sets](#several-arm-sets)
- [Running on an Intel NPU](#running-on-an-intel-npu)
- [Servo health monitoring · power check](#servo-health-monitoring--power-check)
- [Screen language — 한/EN](#screen-language--한en)
- [Troubleshooting](#troubleshooting)

## First setup — setup wizard

**Setup → Start setup wizard**. Choose the arm type and single/bimanual at the top, then click each arm box (four for bimanual); it resumes from the first unfinished step.
No step drives the motors — everything is checked **by moving the arm by hand**.

1. **Find port** — start watching and move only that arm by hand; the port that moved is suggested in green. ⚠️ Torque is off while watching, so support the follower
2. **Diagnose** — motor response · voltage · temperature · protection flags. If a motor is missing, run **motor ID setup** right there
3. **Calibrate** — SO-ARM101: move each joint slowly to its limits. OMX: one **Write factory values** button.
   An SO-ARM101 already calibrated on another PC can skip this with **Import from board**
4. **Verify** — if the 3D model moves together with the real arm when you move it by hand, click **Matches — done**

| Calibration (SO-ARM101) | Verify |
|---|---|
| ![](img/wizard_calib.png) | ![](img/wizard_verify.png) |

| OMX write factory values | OMX verify |
|---|---|
| ![](img/wizard_omx_factory.png) | ![](img/wizard_omx_verify.png) |

> ⚡ **Power first** — do not swap the follower and leader power adapters. Check the rated voltage on the servo label or kit spec
> (the STS3215 comes in separate 7.4 V and 12 V versions). If the voltage in the diagnose step differs from the adapter, stop there.

For cameras, scan in **Setup → 3 · Cameras**; each device takes a thumbnail so you can see which camera is which before registering it.

## Using each screen

The tabs at the top follow the work order: **Setup · Calibration · Control · Collect · Datasets · Training · Models · Rollout**,
with Projects · Hub · Jobs under **More**. If something has to be done first (e.g. no port set, no dataset),
the start button on that screen is disabled and a **large button shows where to go**.

### Setup

**Start setup wizard** at the top is the way to connect arms the first time; this screen is for registering cameras and environments and for detailed settings.

| Section | What it does |
|---|---|
| 0 · Environment | Save and switch named hardware configurations (arm type · mode · ports · calibration ids · cameras) |
| 1 · Robot model · Mode | SO-ARM101 / OMX, single / bimanual. Per-arm port · calibration id · 3D placement |
| 2 · USB serial ports | Port scan, **Port watch** to tell leader from follower. Motor ID setup and arm check are in the folded sections (2b · 2c) |
| 3 · Cameras | Scan (thumbnails) · register, resolution · fps · format — use **MJPG** if several USB cameras on one hub drop out |
| 4 · Other | fps, max move per step (`max_relative_target`), default task |

Changes take effect after **Save settings**.

![Setup](img/setup.png)

### Calibration

Use it to calibrate on its own and to **manage calibration files**. ⚠️ Torque turns off when it starts — support the follower.
**Calibration files** below lists every arm file on this PC, with zip **Export / Import**. Remove unused files with **Delete** (a copy is kept as `.json.bak` in the same folder).

![Calibration](img/calib.png)

### Control

Manual control of the follower. It connects when you open the tab and turns torque off and disconnects when you leave.

- **Torque ON**, then move with the sliders; **Leader follow** makes it follow the leader; **E-STOP** releases torque immediately
- Joints that cannot follow the command for more than a second (blocked · protection) and overheating are shown in red. With two arms, an arm whose torque did not turn on gets its own warning
- Moving to another tab or closing it turns torque off and disconnects — collection and rollout can only start after that

![Control](img/control.png)

### Collect

Enter the collection settings and start. **Recording does not start by itself** — start it with a key while waiting.

| Key | Action |
|---|---|
| `Space` · `→` · `s` | Start recording (while waiting) / save and next (while recording) |
| `←` · `Backspace` · `r` | Discard and redo (while recording) |
| `Esc` | Finish collecting |

- **Beeps** signal start, save and the last 3 seconds, so you do not need to watch the screen
- You can also **continue recording** into an existing dataset
- With **Compress video while recording** (on by default on Windows), saving an episode finishes almost immediately. Episodes with dropped frames (CPU could not keep up) are discarded
- The screen shows the actual **collect rate** (e.g. `29.8 / 30 Hz`) and warns when it is below the target — then the recorded time runs faster than real time

| Collect settings | Collecting |
|---|---|
| ![](img/collect.png) | ![](img/collect_run.png) |

### Datasets

The home screen. The **Getting started** guide at the top shows the remaining steps (it disappears when done or hidden); the dataset list is below.
Click a name to open the review screen — all cameras played on one clock, 3D playback and joint graphs.

- ← / → episode, Space play, **, / .** one frame, **X** mark bad → delete in bulk
- Download · **Import** from another device · **Repair** an interrupted dataset

| List | Review |
|---|---|
| ![](img/datasets.png) | ![](img/review.png) |

### Training

Choose a dataset and a **policy** and train. Loss, grad norm and learning-rate graphs and **progress · time left** are shown live.

| Policy | When |
|---|---|
| **ACT** (default) | Around 50 demos, light and fast — runs on the Intel NPU |
| **ACT2** (experimental) | ACT with switchable image augmentation, chunk interpolation, DINOv2 backbone and flow head for comparison — runs on the Intel NPU |
| **Diffusion** | Tasks with several valid motions |
| **SmolVLA** | Fine-tunes a 450M pretrained model that uses the task description (language) (GPU recommended) |

- A dataset/policy mismatch is flagged in red **before starting** (camera resolution, single/bimanual, etc.)
- 10 episodes and a few thousand steps only check the workflow. For the arm to actually grasp, you need **about 50 demos** and enough steps (tens of thousands) — that takes long on an integrated GPU, so HF Jobs is recommended
- Choose **HF Jobs** under **Run on** to train on Hugging Face cloud GPUs (paid; the hourly price is shown in the list)
- Details per policy: **[POLICIES.md](POLICIES.md)** (Korean)

![Training](img/training.png)

### Models

One card per trained model — steps, last loss, **real-arm success rate**, OpenVINO conversion status.
Each checkpoint has **Run · OpenVINO convert · Export · Delete**, each card has **Resume training**. **Import** a model from another device at the top.

![Models](img/models.png)

### Rollout

Choose a checkpoint and run autonomously. Before starting, cameras and joint count are compared with the current setup and a mismatch is blocked before the arm moves.

- Live view: cameras · 3D arm · measured/commanded/difference per joint · control period · inference time
- Press **Success (S) / Fail (F)** for each attempt to build up a success rate per checkpoint
- On failure, the log is analysed for a **Likely cause** (motor not responding · overload · port · camera · calibration, etc.)

| Start | Running |
|---|---|
| ![](img/rollout.png) | ![](img/rollout_live.png) |

### More — Projects · Hub · Jobs

- **Projects** — groups one task (task description, its own environment, datasets, models). A new project creates an empty environment with the same name
  (only robot model and single/bimanual are set; ports and cameras are empty; calibration ids are project-specific such as `<project>_follower`)
  and takes you to the setup wizard to connect from scratch. Opening a project switches to its environment, other screens show that project,
  and new datasets and models are added to it automatically. Deleting a project deletes its environment too (datasets, models and calibration files are kept).
  Everything else works without one.
- **Hub** — exchange with the Hugging Face Hub: login (token), upload datasets, download datasets and models, check and cancel **HF Jobs cloud training**.
- **Jobs** — background jobs such as collection, training and rollout, with logs. They can be stopped here.

| Projects | Hub |
|---|---|
| ![](img/projects.png) | ![](img/hub.png) |

## Several arm sets

Calibration files are saved under the **calibration id**. Give each set a different id and the files never mix.

1. Label the arms (e.g. `b2`), change the calibration id in **Setup → 1 · Robot model · Mode** to something like `b2_follower_left`, then **Save settings**
   - Changing only the id of the same arm (same port) **copies the existing calibration file to the new name** automatically
   - For bimanual, the left and right arm of the same role must share the prefix (`b2_follower` + `_left` / `_right`)
2. Save this configuration as e.g. `bench2` in **Setup → 0 · Environment** — from then on, switching the environment uses that set's files
3. To move to another PC, use **Calibration → Export (zip)** → **Import** on the other PC. For SO-ARM101 you can also move just the arm and use **Import from board**

## Running on an Intel NPU

On an Intel Core Ultra PC, setup, collection and review work the same, and **ACT · ACT2** models are converted to OpenVINO to run on the **NPU**.

1. Training — train on this PC's integrated GPU (XPU, slow), or **Models → Import** a model trained on HF Jobs (cloud) or an NVIDIA device
2. **Models → OpenVINO convert** (FP16 · INT8) — shows error and inference time for each of NPU · GPU · CPU
3. **Rollout → inference engine: OpenVINO · NPU**

Details and measurements: **[INTEL.md](INTEL.md)** (Korean)

## Servo health monitoring · power check

OpenMimic watches the points where low-cost servo arms actually fail: swapped adapters, overheating and power sag.

- **Power check before starting** — when Control, Collect or Rollout starts, the servo voltage of each arm is read.
  - **Blocked**: 12V on an SO-ARM101 leader (adapters swapped), or above the servo's own voltage limit
  - **Warning**: follower on 5V, out of range, or very different from last time
- **Monitoring while running** — the follower's temperature, voltage and protection flags are read every second.
  - **Rollout**: **stops automatically** (same as the stop button) near the servo temperature limit, on a large voltage drop or a servo protection trip
  - **Collect**: under the same conditions, the episode being recorded is discarded and collection ends
  - **Control**: torque stays on so the arm does not drop; only leader follow and sliders stop
- Only the limits stored in the servo and sourced values are used — details in [DEVELOPMENT.md](DEVELOPMENT.md) (Korean)

## Screen language — 한/EN

The **한/EN** button in the top bar switches Korean ↔ English. Text you type yourself (task descriptions, etc.) is not translated.

![English screen](img/english.png)

## Troubleshooting

| Symptom | Check |
|---|---|
| No port shown | Replug USB; did you log in again after installing (`dialout` in `groups`). On Windows, check that it appears under **Ports (COM & LPT)** in Device Manager and the driver (CH340/CH343 · CP210x) |
| Windows: `'pwsh' is not recognized` | PowerShell 7 is not installed — use `powershell`, or `OpenMimic.bat` |
| Windows: script blocked (execution policy) | Prefix it like `powershell -ExecutionPolicy Bypass -File .\openmimic.ps1 start`, or use `OpenMimic.bat` |
| Windows: other PCs or tablets cannot connect | Allow it in the firewall prompt shown at first start — or allow Python in Settings → Privacy & security → Windows Security → Firewall |
| Windows: camera does not open | Settings → Privacy → **Camera**: allow desktop apps; check that no other program (video calls etc.) is using it |
| Windows: collect rate warning | Camera format MJPG, spread cameras over different USB ports. If still low, the real-time scan exclusion in [INSTALL.md](INSTALL.md) |
| Arms or cameras swap after a reboot (Windows) | Boards and cameras without a serial number follow the **USB socket** they are plugged into — always use the same socket. If cameras only show up by number, `pip install pygrabber` |
| "No calibration file" | Setup wizard step ③ or the Calibration tab. For a file from another PC, Calibration → Import |
| No 3D view | 3D is loaded from the internet (CDN). Without internet only the 3D box is missing; the rest works |
| Several cameras drop out | Setup → Cameras **format** to MJPG, and plug cameras into the PC's own ports |
| "Another job is running" / "Running: manual-control" | The Control tab is open (also on another device or browser — closing it releases the arms), or collection · port watch · calibration is running — finish it in More → Jobs |
| Dataset shows "not finalized" | **Repair** in the dataset list |
| Rollout ends immediately | **Likely cause** on the Rollout screen and the log |
| "The policy uses camera …" / "joint count …" | Camera names or single/bimanual differ from training — make the setup match training |
| Screen looks wrong after an update | `./openmimic.sh restart` (on Windows `OpenMimic.bat restart`) + hard refresh in the browser (Ctrl+Shift+R) |
| "The calibration folder is not the install folder …" at the top | It was started without `activate.sh`. Stop it and start again with `./openmimic.sh start` (on Windows `OpenMimic.bat`) |
| "Power check failed — not started" | The leader and follower adapters are swapped or the voltage is outside the servo limit. **Disconnect power first** and check the adapter labels |
| "Stopped automatically due to a servo fault" | For overheating, let it cool and look for jams or overload; for a voltage drop, check the adapter current rating and connectors |
