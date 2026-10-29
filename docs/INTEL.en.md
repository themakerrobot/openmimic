# Intel Core Ultra PC — OpenVINO inference · XPU training

<a href="INTEL.md">한국어</a> · <b>English</b>

On Intel Core Ultra laptops and mini PCs (Ubuntu · Windows), setup · calibration · control · collection · review work exactly as on NVIDIA devices (Thor etc.),
and inference runs on the **NPU** through OpenVINO. The baseline is **Meteor Lake (Core Ultra series 1) or newer**.
OpenVINO conversion currently supports the **ACT · ACT2** policies.

## 1. Installation

Running the [install](INSTALL.md) (Korean) as is selects the `intel` platform and installs OpenVINO as well.
The script does not install NPU or GPU drivers; it only checks that OpenVINO actually sees the devices.

```bash
source ~/project/openmimic/activate.sh
python -c "import openvino as ov; print(ov.Core().available_devices)"   # ready when it prints ['CPU', 'GPU', 'NPU']
groups                                                                   # you need render to use the NPU and GPU (after logging in again)
```

- If the NPU is missing: the kernel `intel_vpu` driver + [linux-npu-driver](https://github.com/intel/linux-npu-driver/releases)
  (check the release notes for the Ubuntu and kernel combinations)
- If the GPU is missing: [compute-runtime](https://github.com/intel/compute-runtime/releases) (OpenCL / level-zero)
- On Ubuntu 26.04 + Core Ultra, the default install alone found `['CPU', 'GPU', 'NPU']` (checked on real hardware).
- On Windows (`win-intel`), `install.ps1` installs OpenVINO and runs the same check.
  The NPU needs the **Neural processors** (Intel AI Boost) driver in Device Manager, and the GPU needs the Intel graphics driver.
  On real Windows hardware (Lunar Lake · Core Ultra 200V · Arc 140V · 32 GB RAM), XPU training → **OpenVINO FP16 · INT8 conversion → NPU rollout** (bimanual SO-ARM101) was confirmed.

## 2. Importing a model

On the device where you trained, **Export** the checkpoint in the **Models** tab, then bring it in with **Models → Import model** on the Intel device.
If you also download → import the dataset used for training (in **Datasets**), the conversion is validated on real frames and INT8 can be produced.

## 3. Conversion

In the **Models** tab, **OpenVINO convert** on a checkpoint row selects that checkpoint in the conversion box below. Press **OpenVINO convert** to convert (tens of seconds).
For each device on this machine it measures the **maximum error (in joint units)** against the PyTorch result and the **inference time (mean · p95)**, and shows them in a table.
It is **OK** when p95 is within the frame budget (33 ms at 30 fps) and the error is small.

![OpenVINO conversion](img/models_ov.png)

## 4. Inference

On the **Rollout** tab, choose the inference engine **OpenVINO · NPU** and start. Check the engine badge on the rollout screen (e.g. `OpenVINO · NPU · fp16`)
and the actual inference time.

![Rollout — OpenVINO](img/rollout_ov.png)

(The capture above comes from a test server without an NPU, so only the CPU row appears. On a Core Ultra the NPU · GPU rows appear too.)

Good to know
- If the chosen device cannot be used, it falls back in the order **NPU → GPU → CPU** and writes a prominent line in the job log saying it runs on CPU instead of the requested NPU.
- The camera resolution is **fixed to the training data** (the NPU only accepts fixed sizes). If the camera names or resolutions in Setup differ,
  the start is refused before the arm moves.
- If you retrain or overwrite a checkpoint, it shows **OV (needs reconversion)** and the start is blocked — convert it again.
- INT8 can be faster but the error grows. Choose by the error and verdict in the table. The default is FP16.
- FP16 · INT8 conversion and NPU rollout were confirmed on real Windows hardware (Lunar Lake). On Ubuntu + Core Ultra, OpenVINO NPU detection and XPU training are confirmed on real hardware.

## Training on Intel devices

| Method | How | Notes |
|---|---|---|
| **This device's Intel GPU (XPU)** | PyTorch XPU wheel → run location "this device" | lerobot picks the XPU automatically. Confirmed on Ubuntu · Windows; slow (see measurements below) |
| **HF Jobs** | Choose an HF Jobs GPU under **Run on** in the Training tab | Independent of the device. Paid (per hour), Hub login required. When done, download the model in the Hub tab → convert |
| **NVIDIA device** | Train on a Thor etc. → export in the Models tab → import on this device | Step 2 above |
| This device's CPU | The default install, "this device" | Very slow. For checking that it works |

### Switching to XPU (no reinstall; stop OpenMimic first)

```bash
source ~/project/openmimic/activate.sh
pip uninstall -y torch torchvision
pip install "torch<2.12" "torchvision<0.27" --index-url https://download.pytorch.org/whl/xpu
python -c "import torch; print(torch.__version__, torch.xpu.is_available())"     # success when it prints 2.11.x+xpu True
```

To revert: the same commands with `whl/cpu` instead of `whl/xpu`. To use it from a fresh install: `OPENMIMIC_PLATFORM=intel OPENMIMIC_TORCH=xpu ./lerobot_conda.sh`.

Windows (PowerShell, OpenMimic stopped, in the repo folder):

```powershell
. .\activate.ps1
pip uninstall -y torch torchvision
pip install "torch<2.12" "torchvision<0.27" --index-url https://download.pytorch.org/whl/xpu
python -c "import torch; print(torch.__version__, torch.xpu.is_available())"
```

To use it from a fresh install: `$env:OPENMIMIC_TORCH='xpu'; powershell -ExecutionPolicy Bypass -File .\install.ps1`.

### Measurements

| Conditions | Result |
|---|---|
| Ubuntu 26.04 + Core Ultra integrated GPU `Intel(R) Graphics`, torch 2.11.0+xpu | `torch.xpu.is_available()` True, OpenVINO `CPU·GPU·NPU` unchanged |
| ACT 52M · batch 8 · XPU | **about 1.17 s/step** (`updt_s 1.166`, `data_s 0.009` — GPU compute is the bottleneck) |
| Converted | 2000 steps about 39 min · 80000 steps about 26 h |
| Windows 11 + Core Ultra 200V `Arc 140V`, torch 2.11.0+xpu · ACT · 3 cameras (640×480) · batch 8 | **about 2.28 s/step** (`updt_s 2.28`, `data_s 0.024`) — 5000 steps in 3 h 15 min, 80000 steps about 50 h |

Use this device for checking the workflow and short training; for full training (tens of thousands of steps), an NVIDIA device or HF Jobs is recommended.

- Trained checkpoints can be used as they are for OpenVINO conversion and NPU inference (conversion is always checked against the PyTorch result on the CPU).
- The integrated GPU shares system RAM. If memory runs out, lower the batch size (how far it goes is not yet measured).
