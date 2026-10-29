<h1 align="center">OpenMimic</h1>

<p align="center">
  <a href="README.md">한국어</a> · <b>English</b>
</p>

<p align="center">
  <b>Robot arm imitation learning, in one browser tab.</b><br>
  From setup to data collection · training · NPU rollout — no commands<br>
  <b>On-device by default</b> — pick a cloud GPU only when training is too heavy
</p>

<p align="center">
  SO-ARM101 · ROBOTIS OMX · single / bimanual &nbsp;·&nbsp; Ubuntu · native Windows &nbsp;·&nbsp; Intel NPU · NVIDIA · Jetson Thor
</p>

<p align="center">
  <img alt="Windows native" src="https://img.shields.io/badge/Windows-native-0078D6">
  <img alt="Single and bimanual" src="https://img.shields.io/badge/arms-single%20%7C%20bimanual-2ea44f">
  <img alt="Intel NPU" src="https://img.shields.io/badge/Intel%20NPU-OpenVINO%20FP16%20%7C%20INT8-0071C5">
</p>

<p align="center">
  <img src="docs/img/arms.gif" alt="SO-ARM101 and OMX" width="880">
</p>

---

Built on HuggingFace [lerobot](https://github.com/huggingface/lerobot).
Start OpenMimic on the PC the arms are plugged into, then open it from a laptop or tablet browser on the same network — that's it.

## Four steps, one screen

| ① Setup | ② Collect | ③ Train | ④ Run |
|:---:|:---:|:---:|:---:|
| ![](docs/img/wizard_diag.png) | ![](docs/img/collect_run.png) | ![](docs/img/training.png) | ![](docs/img/rollout_live.png) |
| Move the arm by hand — ports and calibration are done | Demonstrate with the leader arm, record with one key | ACT · ACT2 · Diffusion · SmolVLA | Autonomous run with cameras · 3D in view |

## What makes it different

- **Zero commands** — finding ports, motor diagnosis, calibration, recording, training and rollout all happen on screen. The setup wizard guides you step by step.
- **On-device by default** — setup · collect · training · rollout all run on one PC next to the robot, and the data stays there. After installation it works without internet (except the 3D view — see the [user guide](docs/GUIDE.en.md#troubleshooting)).
- **NPU rollout** — trained models are converted to OpenVINO **FP16 · INT8** and run on the Intel Core Ultra **NPU**.
  The conversion measures error against PyTorch and inference time on each device.
- **Cloud GPU when you need it** — if training takes too long on your PC, rent a Hugging Face cloud GPU (HF Jobs) by the hour just for that and start it with one click
  (from $0.40/h for a T4; prices are shown on the training screen). Only then is the dataset uploaded to a private repo in your Hugging Face account; download the finished model and run it on the NPU.
- **Ubuntu and Windows on equal terms** — Windows runs natively, without WSL. Datasets, calibration and models move between the two as they are.
- **Single or bimanual** — SO-ARM101 and ROBOTIS OMX, one arm or two. Switching is one button.
- **Choose where to train** — your PC's NVIDIA GPU, a Jetson Thor, an Intel integrated GPU, or a cloud GPU.
- **Safety for the arms** — a power check before starting blocks swapped adapters, and servo temperature and voltage are watched during a run with automatic stop.
- **Good data** — actual collect rate on screen, episode review with synced cameras · 3D · joint plots, bulk delete of bad episodes, repair of broken datasets.
- **Real-world success rate** — press S / F during a run to compare checkpoints by success rate.
- **한/EN** — switch the screen language with one button.

## Supported

| | |
|---|---|
| Arms | **SO-ARM101** (Feetech STS3215) · **ROBOTIS OMX** (Dynamixel) — single / bimanual |
| OS | **Ubuntu** 22.04 · 24.04 · 26.04 · **Windows** 10 · 11 (native) |
| Training | On-device: NVIDIA GPU · Jetson Thor · Intel integrated GPU (XPU) · Optional: HF Jobs cloud GPU (by the hour) |
| Rollout (on-device) | Intel Core Ultra **NPU** / GPU / CPU (OpenVINO FP16 · INT8) · NVIDIA GPU · Jetson Thor |
| Policies | ACT · ACT2 · Diffusion · SmolVLA — [comparison](docs/POLICIES.md) (Korean) |

> **Checked on real hardware** — one Lunar Lake laptop (Core Ultra 200V · 32 GB RAM · Windows 11) with bimanual SO-ARM101 and 3 cameras,
> all the way from setup → collect → train → OpenVINO FP16 / INT8 conversion → **NPU rollout**.

## Get started

**Ubuntu**

```bash
git clone https://github.com/themakerrobot/openmimic.git ~/project/openmimic && cd ~/project/openmimic
./lerobot_conda.sh          # install — log out and back in once when it finishes
./openmimic.sh start        # start — prints the address to open
```

**Windows** (PowerShell)

```powershell
git clone https://github.com/themakerrobot/openmimic.git $HOME\project\openmimic; cd $HOME\project\openmimic
powershell -ExecutionPolicy Bypass -File .\install.ps1     # install
.\OpenMimic.bat                                            # start — next time, just double-click it in Explorer
```

Open **`http://<PC IP>:8080`** in a browser and the setup wizard guides you through the rest.

## Documents

| Document | Contents |
|---|---|
| [User guide](docs/GUIDE.en.md) | Setup wizard, each screen, several arm sets, safety features, troubleshooting |
| [Install · operation](docs/INSTALL.md) (Korean) | Install per OS, start/stop/autostart, updates, file locations, security |
| [Intel NPU](docs/INTEL.en.md) | OpenVINO conversion · NPU rollout, Intel integrated GPU training, measurements |
| [Policies](docs/POLICIES.md) (Korean) | ACT · ACT2 · Diffusion · SmolVLA comparison, bimanual support |
| [Windows support](docs/WINDOWS_PORT.en.md) | How OS differences are handled, real-hardware check log |
| [Development](docs/DEVELOPMENT.md) (Korean) | Structure, design reasons, config format, decision criteria |

## License

- Code: MIT — [LICENSE](LICENSE)
- `urdf/` SO-101 URDF·STL: [TheRobotStudio SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100) (Apache 2.0) — [urdf/LICENSE.md](urdf/LICENSE.md)
- `urdf/omx_f.urdf`, `urdf/open_manipulator_description/`: [ROBOTIS open_manipulator](https://github.com/ROBOTIS-GIT/open_manipulator) (Apache 2.0) — [urdf/LICENSE-omx.md](urdf/LICENSE-omx.md)
- `plugins/`: bimanual OMX follows the structure of lerobot `bi_so_follower` / `bi_so_leader`; ACT2 builds on lerobot ACT (Apache 2.0)
- `tools_dsrepair.py`: adapted from [huggingface/leLab](https://github.com/huggingface/leLab) `dataset_repair.py` (Apache 2.0, noted in the file header)
