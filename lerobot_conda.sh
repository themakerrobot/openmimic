#!/usr/bin/env bash
# ============================================================================
#  LeRobot + OpenMimic 환경 셋업 (conda, docker 미사용)
#  세 가지 플랫폼을 자동 분기합니다.
#    intel : x86_64 + NVIDIA 없음 (Intel Core Ultra — Meteor Lake 이상 권장)
#            → PyTorch CPU 휠 + OpenVINO/NNCF. 추론은 NPU/GPU/CPU 를 OpenVINO 로 씁니다.
#            학습은 기본(CPU 휠)으로는 매우 느립니다 — 실험적 XPU 휠, HF Jobs 클라우드 학습, NVIDIA 기기 중에서 고르세요.
#            실험적: OPENMIMIC_TORCH=xpu 면 PyTorch XPU 휠을 받아 내장 Arc GPU 로 학습합니다.
#    thor  : Jetson Thor (aarch64 / CUDA 13)
#    cuda  : x86_64 + NVIDIA GPU (CUDA 13)
#  강제 지정: OPENMIMIC_PLATFORM=intel ./lerobot_conda.sh   (intel | thor | cuda)
#  Intel GPU 학습(실험적): OPENMIMIC_PLATFORM=intel OPENMIMIC_TORCH=xpu ./lerobot_conda.sh
#
#  새 기기에서:
#     mkdir -p ~/project && cd ~/project
#     git clone https://github.com/themakerrobot/openmimic.git openmimic
#     cd openmimic
#     chmod +x lerobot_conda.sh
#     sudo -v
#     nohup ./lerobot_conda.sh > /dev/null 2>&1 &
#     sleep 3; tail -f lerobot_conda.log
#
#  끝나면 (재로그인 후):
#     ~/project/openmimic/openmimic.sh start      # 켜기 — 접속 주소 출력 (stop · status · log · update · autostart on)
#     → 브라우저로 열면 셋업 마법사부터 안내합니다
#
#  다른 사람 환경을 건드리지 않습니다 (여러 사람이 쓰는 PC 기준):
#   - conda(miniforge)는 ~/miniforge3 에 '설치만' 합니다. conda init 을 하지 않으므로 ~/.bashrc 가 바뀌지 않고,
#     터미널을 열어도 conda 가 켜지지 않습니다. ~/.condarc 도 쓰지 않습니다.
#   - 파이썬 패키지(torch·lerobot·openvino …)는 전부 conda 환경 'openmimic' 안에만 들어갑니다. 시스템 python·pip 는 그대로.
#   - 쓸 때만 source ~/project/openmimic/activate.sh 로 켭니다 (그 터미널에서만). 데이터·HF 캐시·torch 캐시도 레포 data/ 안.
#   - 시스템에 하는 일은 이것뿐: apt 기본 도구/런타임 몇 개, 시리얼 보드 udev 권한 규칙, 내 계정을 dialout·video(·render) 그룹에 추가.
#
#  핵심 주의사항 (Thor):
#   1) PyTorch 를 pip 기본 인덱스에서 받으면 CUDA 를 못 잡습니다.
#      aarch64-sbsa / CUDA 13 전용 휠 인덱스를 써야 합니다.
#   2) 그 휠이 cp312 라서 conda 파이썬도 3.12 여야 합니다.
# ============================================================================
set -Eeuo pipefail

# ------------------------------- 설정 ---------------------------------------
WORKDIR="${HOME}/project/openmimic"          # 이 레포 (main.py 가 있는 곳)
LOGFILE="${WORKDIR}/lerobot_conda.log"
CONDA_DIR="${HOME}/miniforge3"
ENV_NAME="openmimic"
PY_VER="3.12"                              # Thor 휠이 cp312. 바꾸지 말 것
LEROBOT_SRC="${WORKDIR}/lerobot-src"
LEROBOT_COMMIT="e40b58a8dfa9e7b86918c374791599d070518d11"   # README 와 동일. OpenMimic 이 이 API 에 맞춰져 있음
DATA_DIR="${WORKDIR}/data"

ARCH="$(uname -m)"
TORCH_FLAVOR="${OPENMIMIC_TORCH:-cpu}"        # intel 전용: cpu(기본) | xpu(실험적 — 내장/외장 Intel GPU 로 PyTorch 학습)
PLATFORM="${OPENMIMIC_PLATFORM:-}"
if [[ -z "${PLATFORM}" ]]; then
  if [[ "${ARCH}" == "aarch64" ]]; then
    PLATFORM="thor"
  elif command -v nvidia-smi >/dev/null 2>&1 || lspci 2>/dev/null | grep -qi 'nvidia'; then
    PLATFORM="cuda"
  else
    PLATFORM="intel"
  fi
fi
case "${PLATFORM}" in
  thor)
    MINIFORGE="Miniforge3-Linux-aarch64.sh"
    TORCH_INDEXES=(
      "https://pypi.jetson-ai-lab.io/sbsa/cu130"
      "https://pypi.jetson-ai-lab.io/sbsa/cu129"
      "https://pypi.jetson-ai-lab.dev/sbsa/cu130"
    )
    TORCH_PKGS="torch torchvision torchaudio"
    ;;
  cuda)
    MINIFORGE="Miniforge3-Linux-x86_64.sh"
    TORCH_INDEXES=("https://download.pytorch.org/whl/cu130")
    # lerobot e40b58a 가 torch<2.12 / torchvision<0.27 을 요구합니다 — 최신 휠을 받으면 4-1 에서 충돌
    TORCH_PKGS="torch<2.12 torchvision<0.27"
    ;;
  intel)
    [[ "${ARCH}" == "x86_64" ]] || { echo "intel 플랫폼은 x86_64 전용입니다 (현재 ${ARCH})"; exit 1; }
    MINIFORGE="Miniforge3-Linux-x86_64.sh"
    case "${TORCH_FLAVOR}" in
      cpu) TORCH_INDEXES=("https://download.pytorch.org/whl/cpu" "https://pypi.org/simple") ;;
      # XPU 휠이 없거나 못 받으면 CPU 휠로 내려갑니다 (설치가 멈추지 않게). 실제로 무엇이 깔렸는지는 3-1 에서 표시
      xpu) TORCH_INDEXES=("https://download.pytorch.org/whl/xpu" "https://download.pytorch.org/whl/cpu" "https://pypi.org/simple") ;;
      *) echo "OPENMIMIC_TORCH 는 cpu | xpu 중 하나: ${TORCH_FLAVOR}"; exit 1 ;;
    esac
    TORCH_PKGS="torch<2.12 torchvision<0.27"
    ;;
  *) echo "OPENMIMIC_PLATFORM 은 intel | thor | cuda 중 하나: ${PLATFORM}"; exit 1 ;;
esac
# ---------------------------------------------------------------------------

mkdir -p "${WORKDIR}" "${DATA_DIR}"
exec > >(tee -a "${LOGFILE}") 2>&1

log()  { echo -e "\n[$(date '+%F %T')] === $* ==="; }
warn() { echo "[$(date '+%F %T')] !! $*"; }
die()  { echo "[$(date '+%F %T')] XX 치명적 실패: $*"; exit 1; }
trap 'warn "line ${LINENO} 오류. 로그: ${LOGFILE}"' ERR

log "시작. arch=${ARCH} platform=${PLATFORM} 작업경로=${WORKDIR}"
[[ -f "${WORKDIR}/main.py" ]] || die "${WORKDIR}/main.py 가 없습니다. 이 레포를 ~/project/openmimic 에 clone 한 뒤 실행하세요"

# ------------------------------------------------------------- 0. 시스템 의존성
log "0. 시스템 패키지 (최소한만 — 파이썬 패키지는 전부 conda 환경 안에 설치)"
sudo apt-get update -qq
# 하나씩 설치합니다 — 우분투 버전마다 이름이 바뀐 패키지(24.04+ 의 *t64 등)가 있어 한 개 실패로 전체가 멈추지 않게.
#   git·curl         : 소스 받기
#   build-essential·pkg-config : 휠이 없는 파이썬 패키지(evdev 등) 빌드용 컴파일러
#   libgl1·libglib2.0 : OpenCV(pip 휠) 실행 라이브러리 — 데스크톱 우분투엔 보통 이미 있음
#   v4l-utils        : 카메라 확인용 v4l2-ctl (문제 생겼을 때 진단)
# 영상 인코딩은 PyAV 휠에 FFmpeg 가 들어 있어 시스템 ffmpeg 는 설치하지 않습니다.
apt_one() {   # apt_one 이름 [대체이름…] — 앞에서부터 처음 설치되는 것 하나
  local p
  for p in "$@"; do
    if sudo apt-get install -y -qq "${p}" >/dev/null 2>&1; then echo "  apt: ${p}"; return 0; fi
  done
  warn "apt 설치 실패: $* (이 우분투 버전에 없는 이름일 수 있습니다 — 확인 필요)"
}
for p in git curl build-essential pkg-config libgl1 v4l-utils; do apt_one "${p}"; done
apt_one libglib2.0-0t64 libglib2.0-0

# 시리얼(모터 보드)·카메라 권한. udev 심볼릭 링크는 만들지 않습니다 — 포트는 웹 Setup 탭에서 지정합니다.
sudo usermod -aG dialout,video "${USER}" || true
sudo tee /etc/udev/rules.d/99-so101.rules >/dev/null <<'RULES'
# USB-serial 보드 권한 (WCH CH34x / Silicon Labs CP210x / FTDI). 어느 칩인지는 lsusb 로 확인.
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0666", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", MODE="0666", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", MODE="0666", GROUP="dialout"
RULES
sudo udevadm control --reload-rules && sudo udevadm trigger || true

# sudo 가 필요한 일은 전부 여기(시작 직후)서 끝냅니다 — nohup 으로 돌리면 뒤쪽에서는 sudo 인증 시간이 지나 조용히 실패합니다
# Intel NPU·GPU 드라이버는 설치하지 않습니다 (뒤 4-6 에서 있는지만 점검·안내). 장치 접근 권한 그룹만 추가.
if [[ "${PLATFORM}" == "intel" ]]; then
  sudo usermod -aG render,video "${USER}" || true     # /dev/accel(NPU), /dev/dri(GPU)
fi

# --------------------------------------------------------------- 1. miniforge
log "1. miniforge 설치"
if [[ ! -d "${CONDA_DIR}" ]]; then
  curl -fsSL -o /tmp/miniforge.sh \
    "https://github.com/conda-forge/miniforge/releases/latest/download/${MINIFORGE}"
  bash /tmp/miniforge.sh -b -p "${CONDA_DIR}"
else
  echo "이미 설치됨: ${CONDA_DIR}"
fi

# conda init 을 하지 않습니다 — 이 스크립트 안에서만 conda 를 켭니다 (~/.bashrc·~/.condarc 무변경)
# shellcheck disable=SC1091
source "${CONDA_DIR}/etc/profile.d/conda.sh"

# ---------------------------------------------------------------- 2. conda env
log "2. conda 환경 생성 (python ${PY_VER})"
if conda env list | grep -qE "^${ENV_NAME}\s"; then
  echo "환경 이미 존재: ${ENV_NAME}"
else
  conda create -n "${ENV_NAME}" "python=${PY_VER}" -y -q
fi
conda activate "${ENV_NAME}"
python -V
python -m pip install --upgrade pip setuptools wheel

# ------------------------------------------------------------------ 3. PyTorch
log "3. PyTorch 설치 (${PLATFORM})"
# 다시 설치할 때 다른 종류의 휠(예: 2.11.0+cpu)이 남아 있으면 pip 가 "이미 만족" 으로 보고 바꾸지 않습니다 → 먼저 지웁니다
if [[ "${PLATFORM}" == "intel" ]]; then
  OLD_LOCAL="$(python -c "import torch;v=torch.__version__;print(v.split('+')[1] if '+' in v else '')" 2>/dev/null || true)"
  if [[ -n "${OLD_LOCAL}" && "${OLD_LOCAL}" != "${TORCH_FLAVOR}" ]]; then
    echo "--- 기존 torch(+${OLD_LOCAL}) 를 지우고 ${TORCH_FLAVOR} 휠로 바꿉니다"
    pip uninstall -y torch torchvision
  fi
fi
TORCH_OK=0
for idx in "${TORCH_INDEXES[@]}"; do
  echo "--- 인덱스 시도: ${idx}"
  # shellcheck disable=SC2086
  if pip install --index-url "${idx}" ${TORCH_PKGS}; then
    TORCH_OK=1
    echo "--- 성공: ${idx}"
    break
  fi
  warn "실패: ${idx}"
done
(( TORCH_OK == 0 )) && die "PyTorch 설치 실패. 인덱스 경로 확인 후 TORCH_INDEXES 수정"

check_cuda() {
if [[ "${PLATFORM}" == "intel" ]]; then
# TORCH_LOCAL: 3단계에서 실제로 깔린 휠의 꼬리표(xpu / cpu / 없음). 이후 lerobot 설치가 XPU 휠을 CPU 휠로 바꾸면 실패로 봅니다
TORCH_LOCAL="${TORCH_LOCAL:-}" python - <<'PY'
import os, sys, torch
local = torch.__version__.split("+")[1] if "+" in torch.__version__ else ""
want = os.environ.get("TORCH_LOCAL", "")
xpu = hasattr(torch, "xpu") and torch.xpu.is_available()
print("torch      :", torch.__version__, "| xpu:", xpu, "(추론 가속은 OpenVINO 가 담당)")
if xpu:
    print("xpu device :", torch.xpu.get_device_name(0))
elif local == "xpu":
    print("!! XPU 휠이지만 Intel GPU 를 못 잡았습니다 — compute-runtime(level-zero) 드라이버·render 그룹(재로그인) 확인. 학습은 CPU 로 돌아갑니다")
if want == "xpu" and local != "xpu":
    print("!! XPU 휠이 다른 휠로 바뀌었습니다"); sys.exit(1)
PY
return
fi
python - <<'PY'
import torch, sys
print("torch      :", torch.__version__)
print("cuda avail :", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device     :", torch.cuda.get_device_name(0))
else:
    print("!! CUDA 미인식 — 잘못된 휠입니다.")
    sys.exit(1)
PY
}
TORCH_LOCAL="$(python -c "import torch;v=torch.__version__;print(v.split('+')[1] if '+' in v else '')")"
export TORCH_LOCAL
if [[ "${PLATFORM}" == "intel" && "${TORCH_FLAVOR}" == "xpu" && "${TORCH_LOCAL}" != "xpu" ]]; then
  warn "XPU 휠을 받지 못해 ${TORCH_LOCAL:-기본} 휠을 설치했습니다 — Intel GPU 학습 없이 계속합니다 (추론은 OpenVINO 로 그대로 가능)"
fi
log "3-1. torch 확인 (thor/cuda 는 CUDA 인식 필수)"
check_cuda || die "CUDA 미인식. 여기서 멈춥니다"

# ------------------------------------------------------------------ 4. LeRobot
log "4. LeRobot 소스 (commit ${LEROBOT_COMMIT:0:8})"
if [[ ! -d "${LEROBOT_SRC}/.git" ]]; then
  git clone https://github.com/huggingface/lerobot.git "${LEROBOT_SRC}"
fi
git -C "${LEROBOT_SRC}" fetch --all --tags || warn "fetch 실패, 로컬 트리 사용"
git -C "${LEROBOT_SRC}" checkout -q "${LEROBOT_COMMIT}" \
  || die "lerobot commit ${LEROBOT_COMMIT} 체크아웃 실패"
cd "${LEROBOT_SRC}"

log "4-1. lerobot[feetech,dynamixel,training,diffusion,smolvla] 설치 (torch 는 위 휠 유지)"
# torch 를 pip 기본 인덱스 것으로 덮어쓰지 않도록 현재 버전으로 핀.
# 로컬 버전 꼬리표(+cu130 / +cpu)는 뗍니다 — 붙어 있으면 pip 가 "Cannot install None" 으로 해석을 포기합니다.
# (PEP 440: 'torch==2.11.0' 은 설치된 2.11.0+cu130 을 그대로 만족합니다)
python - <<'PY' > /tmp/torch-constraint.txt
import torch, torchvision
print(f"torch=={torch.__version__.split('+')[0]}")
print(f"torchvision=={torchvision.__version__.split('+')[0]}")
PY
cat /tmp/torch-constraint.txt
# feetech = SO-ARM101 (STS3215), dynamixel = ROBOTIS OMX (XL430/XL330)
# diffusion / smolvla = 학습 탭에서 고를 수 있는 정책 (diffusers / transformers)
PIP_CONSTRAINT=/tmp/torch-constraint.txt pip install -e ".[feetech,dynamixel,training,diffusion,smolvla]" \
  || die "lerobot 설치 실패 (로그 확인)"

# torchcodec 은 Jetson 에서 문제를 일으켜 pyav 디코딩으로 통일합니다 (README 와 동일)
pip uninstall -y torchcodec || true
pip install "av>=15.0.0,<16.0.0"

log "4-2. OpenMimic 의존성"
# websockets: Control 탭(/ws/control)이 WebSocket 을 씁니다. uvicorn 만 깔면 WebSocket 이 404 로 거부됩니다
pip install "fastapi<1.0" uvicorn "websockets>=12"

log "4-3. 플러그인 — 양팔 OMX (bi_omx_follower / bi_omx_leader) · ACT2 정책 (act2)"
# lerobot 에 없는 것들을 이 레포의 plugins/ 에서 설치합니다. 설치된 패키지 이름이
# lerobot_robot_* / lerobot_teleoperator_* / lerobot_policy_* 이면 lerobot-record/rollout/train 이 자동으로 읽습니다.
PIP_CONSTRAINT=/tmp/torch-constraint.txt pip install --no-deps \
  -e "${WORKDIR}/plugins/lerobot_robot_bi_omx" -e "${WORKDIR}/plugins/lerobot_teleoperator_bi_omx" \
  -e "${WORKDIR}/plugins/lerobot_policy_act2" \
  || die "플러그인 설치 실패"

log "4-4. torch 가 덮어써지지 않았는지 재확인"
check_cuda || die "lerobot 설치 과정에서 torch 휠이 바뀌었습니다. pip uninstall -y torch torchvision 후 3단계 인덱스로 재설치"

if [[ "${PLATFORM}" == "intel" ]]; then
  log "4-5. OpenVINO / NNCF (Intel NPU·GPU·CPU 추론)"
  PIP_CONSTRAINT=/tmp/torch-constraint.txt pip install "openvino>=2025.4" "nncf>=2.19" \
    || die "openvino 설치 실패"

  log "4-6. Intel NPU / GPU 드라이버 점검 (NPU 사용자 공간 드라이버는 설치하지 않습니다 — 안내만)"
  CPU_NAME="$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
  echo "CPU : ${CPU_NAME}"
  if ! grep -qi 'Core(TM) Ultra' <<<"${CPU_NAME}"; then
    warn "Core Ultra 가 아닙니다 — NPU 가 없을 수 있습니다. OpenVINO GPU/CPU 로만 추론합니다"
  fi
  # 판정은 OpenVINO 가 실제로 장치를 보는지로 합니다 — 패키지 이름은 우분투 버전마다 달라 dpkg 로 보면 틀립니다(26.04 에서 확인)
  OV_DEVS="$(python -c "import openvino as ov; print(' '.join(ov.Core().available_devices))" 2>/dev/null || true)"
  echo "OpenVINO 장치: ${OV_DEVS:-없음}"
  if grep -qw NPU <<<"${OV_DEVS}"; then
    echo "NPU : OpenVINO 에서 사용 가능"
  elif [[ -e /dev/accel/accel0 ]]; then
    warn "NPU 장치(/dev/accel/accel0)는 있지만 OpenVINO 가 못 잡습니다 — 사용자 공간 NPU 드라이버 또는 render 그룹(재로그인) 확인"
    warn "  → https://github.com/intel/linux-npu-driver/releases (우분투 버전별 패키지는 릴리스 노트로 확인 필요)"
  else
    warn "NPU 장치(/dev/accel/accel0) 없음 — 커널 intel_vpu 드라이버와 사용자 공간 NPU 드라이버가 필요합니다. 없으면 GPU/CPU 로 추론"
  fi
  if grep -qw GPU <<<"${OV_DEVS}"; then
    echo "GPU : OpenVINO 에서 사용 가능"
  elif ls /dev/dri/renderD* >/dev/null 2>&1; then
    warn "GPU render 노드는 있지만 OpenVINO 가 못 잡습니다 — Intel GPU 컴퓨트 런타임(level-zero/OpenCL) 또는 render 그룹(재로그인) 확인"
  else
    warn "GPU render 노드(/dev/dri/renderD*) 없음"
  fi
fi

# ----------------------------------------------------------------- 5. 임포트 검증
log "5. 최종 검증"
python - <<'PY'
import torch
print("torch   :", torch.__version__, "| cuda:", torch.cuda.is_available(),
      "| xpu:", hasattr(torch, "xpu") and torch.xpu.is_available())
try:
    import openvino as ov
    print("openvino:", ov.__version__, "| devices:", ov.Core().available_devices)
except ImportError:
    print("openvino: (미설치 — intel 플랫폼에서만 설치)")
import lerobot
from lerobot.robots.so_follower import SOFollower
from lerobot.robots.bi_so_follower import BiSOFollower
from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.robots.omx_follower import OmxFollower
from lerobot.motors.dynamixel import DynamixelMotorsBus
import dynamixel_sdk  # noqa: F401
import lerobot_robot_bi_omx, lerobot_teleoperator_bi_omx  # noqa: F401
from lerobot.scripts.lerobot_record import record_loop
import fastapi, uvicorn, cv2, av, websockets  # noqa: F401  websockets 없으면 Control 탭이 안 됩니다
print("lerobot :", "OK (so_follower / bi_so_follower / feetech / omx / bi_omx / dynamixel / record_loop)")
print("fastapi :", fastapi.__version__, "| cv2:", cv2.__version__, "| av:", av.__version__)
PY

# ------------------------------------------------------------ 6. 활성화 헬퍼
log "6. activate.sh"
# 경로를 $HOME 기준으로 남깁니다 — 절대경로를 박으면 기기마다 파일이 달라져
# git 에서 매번 diff 가 뜨고, 다른 기기에서는 경로가 틀립니다.
h() { printf '%s' "${1/#${HOME}/\$HOME}"; }
cat > "${WORKDIR}/activate.sh" <<EOF
#!/usr/bin/env bash
source "$(h "${CONDA_DIR}")/etc/profile.d/conda.sh"
conda activate ${ENV_NAME}
export HF_HOME="$(h "${DATA_DIR}")/hf"
export TORCH_HOME="$(h "${DATA_DIR}")/torch"
cd "$(h "${WORKDIR}")"
EOF
chmod +x "${WORKDIR}/activate.sh"
chmod +x "${WORKDIR}/openmimic.sh" 2>/dev/null || true

# ------------------------------------------------------------------- 완료
log "완료"
cat <<EOF

  플랫폼    : ${PLATFORM}$( [[ "${PLATFORM}" == "intel" ]] && echo " (torch 휠: ${TORCH_LOCAL:-기본})" || true )
  conda env : ${ENV_NAME}  (python ${PY_VER})
  lerobot   : ${LEROBOT_SRC} @ ${LEROBOT_COMMIT:0:8}
  데이터    : ${DATA_DIR}   (HF_HOME=${DATA_DIR}/hf → 캘리브레이션은 \$HF_HOME/lerobot/calibration)
  활성화    : source ${WORKDIR}/activate.sh

  --- OpenMimic 켜기 (재로그인 후) ---
  ${WORKDIR}/openmimic.sh start       # 접속 주소를 알려 줍니다 (stop · status · log · update · autostart on)

  --- 웹에서 ---
  처음 열면 셋업 마법사가 뜹니다 → 데이터셋 화면의 '시작하기' 순서(셋업 → 캘리브레이션 → 수집 → 학습 → 실행)대로

  * dialout / video 그룹 반영을 위해 재로그인(또는 재부팅) 한 번 필요합니다.
  * intel: 모델 탭 → 체크포인트 "OpenVINO 변환" → 실행 탭에서 추론 엔진 NPU/GPU/CPU 선택.
           render 그룹 반영(NPU/GPU 권한)을 위해 재로그인 필요.
           학습은 학습 탭 실행 위치에서 HF Jobs 를 고르세요 (XPU 휠이면 이 기기 Intel GPU 로도 가능 — 실험적).
  * 학습 시 GPU 를 쓰는 다른 서비스(vLLM 등)는 내리세요.

EOF
