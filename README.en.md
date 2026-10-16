<h1 align="center">OpenMimic</h1>

<p align="center">
  <a href="README.md">한국어</a> · <b>English</b>
</p>

<p align="center">
  <b>Robot arm setup · data collection · training · rollout in one web screen</b><br>
  SO-ARM101 · ROBOTIS OMX · single / bimanual · Intel Core Ultra NPU · Jetson Thor · NVIDIA GPU
</p>

<p align="center">
  <img src="docs/img/arms.gif" alt="SO-ARM101 and OMX" width="880">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#first-setup--setup-wizard">Setup wizard</a> ·
  <a href="#using-each-screen">Using each screen</a> ·
  <a href="#several-arm-sets">Several arm sets</a> ·
  <a href="#running-on-an-intel-npu">Intel NPU</a> ·
  <a href="#troubleshooting">Troubleshooting</a>
</p>

---

OpenMimic lets you work with robot arms through HuggingFace [lerobot](https://github.com/huggingface/lerobot) **in the browser, without commands**.
You can connect from a laptop or tablet on the same network; the arms, cameras and training run on the PC where OpenMimic is started.

The screen switches between Korean and English with the **한/EN** button in the top bar. The screenshots below show the Korean screen.

| ① Setup | ② Collect | ③ Training | ④ Rollout |
|:---:|:---:|:---:|:---:|
| ![](docs/img/wizard_diag.png) | ![](docs/img/collect_run.png) | ![](docs/img/training.png) | ![](docs/img/rollout_live.png) |
| Find ports · diagnose · calibrate by moving the arm by hand | Demonstrate with the leader arm, record and save with one key | ACT · ACT2 · Diffusion · SmolVLA | Autonomous run while watching cameras · 3D · joints, success-rate log |

## Supported hardware

| Arm | Motors | Single | Bimanual | Calibration |
|---|---|:---:|:---:|---|
| **SO-ARM101** (SO-101) | Feetech STS3215 | ✅ | ✅ | Move each joint by hand to its limits |
| **OMX** (ROBOTIS OMX-F / OMX-L) | Dynamixel XL430 / XL330 | ✅ | ✅ | Write factory values (one button) |

| Device | Training | Rollout (inference) |
|---|---|---|
| Intel Core Ultra PC | HF Jobs cloud or integrated GPU (experimental) | ✅ **NPU** / GPU / CPU (OpenVINO, ACT · ACT2) |
| Jetson Thor · NVIDIA GPU PC | ✅ GPU | ✅ GPU |

## Quick start

```bash
mkdir -p ~/project && cd ~/project
git clone https://github.com/themakerrobot/openmimic.git openmimic
cd openmimic && chmod +x lerobot_conda.sh && sudo -v
nohup ./lerobot_conda.sh > /dev/null 2>&1 &
sleep 3; tail -f lerobot_conda.log        # press Ctrl+C when "=== 완료 ===" (done) appears
```

When the install finishes, **log out and log back in once** (serial and camera permissions), then start it.

```bash
~/project/openmimic/openmimic.sh start     # start — prints the address to open
```

| Command | What it does |
|---|---|
| `./openmimic.sh start` · `stop` · `restart` | Start · stop · restart |
| `./openmimic.sh status` | Whether it is running, and the address |
| `./openmimic.sh log` | Show the log (Ctrl+C leaves; OpenMimic keeps running) |
| `./openmimic.sh update` | Get the latest version and restart |
| `./openmimic.sh autostart on` | Start automatically at login (boot) — this account only; `off` turns it off |

Open the address printed by `start` (**`http://<PC IP>:8080`**) in a browser. The **setup wizard** opens the first time;
after that, follow the **Getting started** guide above the dataset list (Setup → Calibration → Collect → Training → Rollout).

- The device type (Intel / Thor / NVIDIA) is detected automatically. Ubuntu 22.04 · 24.04 · 26.04
- **Other users' environments are left alone** — conda is installed but `~/.bashrc` is not touched, and every package goes into the `openmimic` virtual environment
- Updates, platforms and more: **[docs/INSTALL.md](docs/INSTALL.md)** (Korean)

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
| ![](docs/img/wizard_calib.png) | ![](docs/img/wizard_verify.png) |

| OMX write factory values | OMX verify |
|---|---|
| ![](docs/img/wizard_omx_factory.png) | ![](docs/img/wizard_omx_verify.png) |

> ⚡ **Power first** — do not swap the follower and leader power adapters. Check the rated voltage on the servo label or kit spec
> (the STS3215 comes in separate 7.4 V and 12 V versions). If the voltage in the diagnose step differs from the adapter, stop there.

For cameras, scan in **Setup → 3 · Cameras**; each device takes a thumbnail so you can see which camera is which before registering it.

## Using each screen

The tabs at the top follow the work order: **Setup · Calibration · Control · Collect · Datasets · Training · Models · Rollout**,
with Projects · Hub · Jobs under **More**. If something has to be done first (e.g. no port set, no dataset),
the start button on that screen is disabled and a **large button shows where to go**.

### Setup

**Start setup wizard** at the top is the way to connect arms the first time; this screen is for registering cameras and environments and for detailed settings.
If no arms are connected, a **try with virtual devices** card appears below it ([Trying it without arms](#trying-it-without-arms--virtual-devices)).

| Section | What it does |
|---|---|
| 0 · Environment | Save and switch named hardware configurations (arm type · mode · ports · calibration ids · cameras) |
| 1 · Robot model · Mode | SO-ARM101 / OMX, single / bimanual. Per-arm port · calibration id · 3D placement |
| 2 · USB serial ports | Port scan, **Port watch** to tell leader from follower. Motor ID setup and arm check are in the folded sections (2b · 2c) |
| 3 · Cameras | Scan (thumbnails) · register, resolution · fps · format — use **MJPG** if several USB cameras on one hub drop out |
| 4 · Other | fps, max move per step (`max_relative_target`), default task |

Changes take effect after **Save settings**.

![Setup](docs/img/setup.png)

### Calibration

Use it to calibrate on its own and to **manage calibration files**. ⚠️ Torque turns off when it starts — support the follower.
**Calibration files** below lists every arm file on this PC, with zip **Export / Import**.

![Calibration](docs/img/calib.png)

### Control

Manual control of the follower. It connects when you open the tab and turns torque off and disconnects when you leave.

- **Torque ON**, then move with the sliders; **Leader follow** makes it follow the leader; **E-STOP** releases torque immediately
- Joints that cannot follow the command (blocked · protection) and overheating are shown in red

![Control](docs/img/control.png)

### Collect

Enter the collection settings and start. **Recording does not start by itself** — start it with a key while waiting.

| Key | Action |
|---|---|
| `Space` · `→` · `s` | Start recording (while waiting) / save and next (while recording) |
| `←` · `Backspace` · `r` | Discard and redo (while recording) |
| `Esc` | Finish collecting |

- **Beeps** signal start, save and the last 3 seconds, so you do not need to watch the screen
- You can also **continue recording** into an existing dataset

| Collect settings | Collecting |
|---|---|
| ![](docs/img/collect.png) | ![](docs/img/collect_run.png) |

### Datasets

The home screen. The **Getting started** guide at the top shows the remaining steps (it disappears when done or hidden); the dataset list is below.
Click a name to open the review screen — all cameras played on one clock, 3D playback and joint graphs.

- ← / → episode, Space play, **, / .** one frame, **X** mark bad → delete in bulk
- Download · **Import** from another device · **Repair** an interrupted dataset

| List | Review |
|---|---|
| ![](docs/img/datasets.png) | ![](docs/img/review.png) |

### Training

Choose a dataset and a **policy** and train. Loss, grad norm and learning-rate graphs and **progress · time left** are shown live.

| Policy | When |
|---|---|
| **ACT** (default) | Around 50 demos, light and fast — runs on the Intel NPU |
| **ACT2** (experimental) | ACT with switchable image augmentation, chunk interpolation, DINOv2 backbone and flow head for comparison — runs on the Intel NPU |
| **Diffusion** | Tasks with several valid motions |
| **SmolVLA** | Fine-tunes a 450M pretrained model that uses the task description (language) (GPU recommended) |

- A dataset/policy mismatch is flagged in red **before starting** (camera resolution, single/bimanual, etc.)
- Choose **HF Jobs** under **Run on** to train on Hugging Face cloud GPUs (paid; the hourly price is shown in the list)
- Details per policy: **[docs/POLICIES.md](docs/POLICIES.md)** (Korean)

![Training](docs/img/training.png)

### Models

One card per trained model — steps, last loss, **real-arm success rate**, OpenVINO conversion status.
Each checkpoint has **Run · OpenVINO convert · Export · Delete**, each card has **Resume training**. **Import** a model from another device at the top.

![Models](docs/img/models.png)

### Rollout

Choose a checkpoint and run autonomously. Before starting, cameras and joint count are compared with the current setup and a mismatch is blocked before the arm moves.

- Live view: cameras · 3D arm · measured/commanded/difference per joint · control period · inference time
- Press **Success (S) / Fail (F)** for each attempt to build up a success rate per checkpoint
- On failure, the log is analysed for a **Likely cause** (motor not responding · overload · port · camera · calibration, etc.)

| Start | Running |
|---|---|
| ![](docs/img/rollout.png) | ![](docs/img/rollout_live.png) |

### More — Projects · Hub · Jobs

- **Projects** — groups one task (task description, reference environment, datasets, models). When open, other screens show that project,
  and new datasets and models are added to it automatically. Everything else works without one.
- **Hub** — exchange with the Hugging Face Hub: login (token), upload datasets, download datasets and models, check and cancel **HF Jobs cloud training**.
- **Jobs** — background jobs such as collection, training and rollout, with logs. They can be stopped here.

| Projects | Hub |
|---|---|
| ![](docs/img/projects.png) | ![](docs/img/hub.png) |

## Several arm sets

Calibration files are saved under the **calibration id**. Give each set a different id and the files never mix.

1. Label the arms (e.g. `b2`), change the calibration id in **Setup → 1 · Robot model · Mode** to something like `b2_follower_left`, then **Save settings**
   - Changing only the id of the same arm (same port) **copies the existing calibration file to the new name** automatically
   - For bimanual, the left and right arm of the same role must share the prefix (`b2_follower` + `_left` / `_right`)
2. Save this configuration as e.g. `bench2` in **Setup → 0 · Environment** — from then on, switching the environment uses that set's files
3. To move to another PC, use **Calibration → Export (zip)** → **Import** on the other PC. For SO-ARM101 you can also move just the arm and use **Import from board**

## Running on an Intel NPU

On an Intel Core Ultra PC, setup, collection and review work the same, and **ACT · ACT2** models are converted to OpenVINO to run on the **NPU**.

1. Training — train on this PC's integrated GPU (experimental), or **Models → Import** a model trained on HF Jobs (cloud) or an NVIDIA device
2. **Models → OpenVINO convert** — shows error and inference time for each of NPU · GPU · CPU
3. **Rollout → inference engine: OpenVINO · NPU**

Details and measurements: **[docs/INTEL.md](docs/INTEL.md)** (Korean)

## Trying it without arms — virtual devices

You can go from setup to rollout without any arms or cameras — useful for preparing a class or a demo.

1. **Turn on virtual devices** at the top of **Setup** (or the setup wizard) — choose the arm type (SO-ARM101 / OMX) and single/bimanual
2. Virtual arms (leader and follower) and virtual cameras are created, and the environment switches to `sim-<type>-<mode>`.
   Your real arm settings are kept, and calibration files are stored separately so they never mix
3. In the **virtual devices** bar at the bottom, use **Move follower / Move leader** instead of your hand (find port, calibration, verify),
   and **Leader auto-motion** for Control and Collect
4. **Simulate faults** triggers overheating or a voltage drop so you can see how servo health monitoring stops things
5. When done, **Turn off virtual devices** — you return to the previous environment

![Virtual devices](docs/img/sim.png)

### Physics simulation — collect, train and run without arms

For an SO-ARM101 single arm you can turn on virtual devices with **physics simulation (MuJoCo)** (on by default).
The follower becomes an SO-101 inside the simulation, and the cameras (top · wrist) show a table, a red cube and a green square.
The task is **pick up the cube and place it in the green square**.

1. Calibrate in the setup wizard (**Move** buttons in the bar)
2. **Collect** — turn on **Record automatically with virtual demos** and start: for each episode the cube is placed anew,
   the leader demonstrates (about 14 s), and recording continues by itself up to the target number of episodes
3. **Training** — train ACT etc. on the collected dataset (same screen as with real arms)
4. **Rollout** — the cube is placed anew at the start; once it is in the green square the attempt is **recorded as a success automatically**
   and the run ends. A run that ends without success is recorded as a failure — the success rate builds up in the Models tab

![Physics simulation](docs/img/sim_mujoco.png)

- Models trained in simulation are for teaching and practicing the workflow; they are not meant for real arms as is
- Camera images are rendered on the GPU (EGL). Without a GPU they are rendered on the CPU, which is slower

## Servo health monitoring · power check

OpenMimic watches the points where low-cost servo arms actually fail: swapped adapters, overheating and power sag.

- **Power check before starting** — when Control, Collect or Rollout starts, the servo voltage of each arm is read.
  - **Blocked**: 12V on an SO-ARM101 leader (adapters swapped), or above the servo's own voltage limit
  - **Warning**: follower on 5V, out of range, or very different from last time
- **Monitoring while running** — the follower's temperature, voltage and protection flags are read every second.
  - **Rollout**: **stops automatically** (same as the stop button) near the servo temperature limit, on a large voltage drop or a servo protection trip
  - **Collect**: under the same conditions, the episode being recorded is discarded and collection ends
  - **Control**: torque stays on so the arm does not drop; only leader follow and sliders stop
- Only the limits stored in the servo and sourced values are used — details in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (Korean)

## Screen language — 한/EN

The **한/EN** button in the top bar switches Korean ↔ English. Text you type yourself (task descriptions, etc.) is not translated.

![English screen](docs/img/english.png)

## Troubleshooting

| Symptom | Check |
|---|---|
| No port shown | Replug USB; did you log in again after installing (`dialout` in `groups`) |
| "No calibration file" | Setup wizard step ③ or the Calibration tab. For a file from another PC, Calibration → Import |
| No 3D view | 3D is loaded from the internet (CDN). Without internet only the 3D box is missing; the rest works |
| Several cameras drop out | Setup → Cameras **format** to MJPG, and plug cameras into the PC's own ports |
| "Another job is running" | The Control tab is open, or collection · port watch · calibration is running — finish it in More → Jobs |
| Dataset shows "not finalized" | **Repair** in the dataset list |
| Rollout ends immediately | **Likely cause** on the Rollout screen and the log |
| "The policy uses camera …" / "joint count …" | Camera names or single/bimanual differ from training — make the setup match training |
| Screen looks wrong after an update | `./openmimic.sh restart` + hard refresh in the browser (Ctrl+Shift+R) |
| "The calibration folder is not the install folder …" at the top | It was started without `activate.sh`. Stop it and start again with `./openmimic.sh start` |
| "Power check failed — not started" | The leader and follower adapters are swapped or the voltage is outside the servo limit. **Disconnect power first** and check the adapter labels |
| "Stopped automatically due to a servo fault" | For overheating, let it cool and look for jams or overload; for a voltage drop, check the adapter current rating and connectors |

## Documents

The detailed documents are in Korean.

| Document | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | What the install script does, updates, file locations, security, terminal tools |
| [docs/POLICIES.md](docs/POLICIES.md) | Training policy comparison, bimanual support, policies not included |
| [docs/INTEL.md](docs/INTEL.md) | Intel Core Ultra — OpenVINO conversion · NPU inference, XPU training, measurements |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Structure · design reasons · config file format · decision criteria (for developers) |

## License

- Code: MIT — [LICENSE](LICENSE)
- `urdf/` SO-101 URDF·STL: [TheRobotStudio SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100) (Apache 2.0) — [urdf/LICENSE.md](urdf/LICENSE.md)
- `urdf/omx_f.urdf`, `urdf/open_manipulator_description/`: [ROBOTIS open_manipulator](https://github.com/ROBOTIS-GIT/open_manipulator) (Apache 2.0) — [urdf/LICENSE-omx.md](urdf/LICENSE-omx.md)
- `plugins/`: bimanual OMX follows the structure of lerobot `bi_so_follower` / `bi_so_leader`; ACT2 builds on lerobot ACT (Apache 2.0)
- `tools_dsrepair.py`: adapted from [huggingface/leLab](https://github.com/huggingface/leLab) `dataset_repair.py` (Apache 2.0, noted in the file header)
