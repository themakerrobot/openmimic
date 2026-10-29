# 설치 · 업데이트 · 운영

소개는 [README](../README.md), 화면별 사용법은 [GUIDE.md](GUIDE.md) 에 있습니다.

## 빠른 설치

| OS | 설치 | 켜기 |
|---|---|---|
| Ubuntu 22.04 · 24.04 · 26.04 | `lerobot_conda.sh` | `./openmimic.sh start` |
| Windows 10 · 11 (64비트) | `install.ps1` | 레포 폴더의 **`OpenMimic.bat`** 더블클릭 |

켜면 접속 주소가 나옵니다. 브라우저에서 **`http://<PC IP>:8080`** 으로 들어가면 처음엔 **셋업 마법사**가 바로 뜹니다.
같은 네트워크의 노트북·태블릿에서 접속해도 됩니다.

### Ubuntu 22.04 · 24.04 · 26.04

```bash
mkdir -p ~/project && cd ~/project
git clone https://github.com/themakerrobot/openmimic.git openmimic
cd openmimic && chmod +x lerobot_conda.sh && sudo -v
nohup ./lerobot_conda.sh > /dev/null 2>&1 &
sleep 3; tail -f lerobot_conda.log        # "=== 완료 ===" 가 나오면 Ctrl+C
```

설치가 끝나면 **한 번 로그아웃 후 다시 로그인**하고 (시리얼·카메라 권한), 켭니다.

```bash
~/project/openmimic/openmimic.sh start     # 켜기 — 접속 주소를 알려 줍니다
```

| 명령 | 하는 일 |
|---|---|
| `./openmimic.sh start` · `stop` · `restart` | 켜기 · 끄기 · 다시 켜기 (백그라운드) |
| `./openmimic.sh status` | 켜져 있는지와 접속 주소 |
| `./openmimic.sh log` | 로그 보기 (Ctrl+C 로 나가도 OpenMimic 은 계속 돎) |
| `./openmimic.sh update` | 최신 버전 받고 다시 켜기 |
| `./openmimic.sh autostart on` | 로그인(부팅) 때 자동으로 켜기 — 이 계정만, `off` 로 끔 |

### Windows 10 · 11 (64비트, 네이티브)

WSL 없이 Windows 에서 바로 돕니다. SO-ARM101 · OMX 한팔·양팔 모두 Ubuntu 와 같게 지원합니다.
PowerShell 에서 (git 이 없으면 먼저 `winget install --id Git.Git -e`). 관리자 권한은 필요 없습니다.

```powershell
git clone https://github.com/themakerrobot/openmimic.git $HOME\project\openmimic; cd $HOME\project\openmimic
powershell -ExecutionPolicy Bypass -File .\install.ps1      # "=== 완료 ===" 가 나올 때까지 (로그: lerobot_conda.log)
```

설치가 끝나면 탐색기에서 레포 폴더(`%USERPROFILE%\project\openmimic`)의 **`OpenMimic.bat` 을 더블클릭**합니다.
바탕화면에 두려면 파일을 옮기지 말고 **바로 가기**를 만드세요. 처음 켤 때 Windows 방화벽 창이 뜨면 **허용**해야 다른 PC·태블릿에서 접속됩니다.

| 명령 (cmd 에서 레포 폴더로 이동해서 · PowerShell 은 `.\OpenMimic.bat …`) | 하는 일 |
|---|---|
| `OpenMimic.bat` (더블클릭) | **그 창에서** 켜기 — 로그가 창에 보이고 **Ctrl+C** 로 끔 |
| `OpenMimic.bat update` | 최신 버전 받고 그 창에서 다시 켜기 |
| `OpenMimic.bat stop` · `restart` · `status` | 끄기 · 다시 켜기 · 켜져 있는지와 접속 주소 |
| `OpenMimic.bat background` · `log` | 창 없이 켜기 · 그 로그 보기 |
| `OpenMimic.bat autostart on` | 로그인 때 최소화된 창으로 켜기 — 이 계정만, `off` 로 끔 |

- 끌 때는 **Ctrl+C** — 수집·학습 같은 작업은 따로 돌아 계속 진행되고, 다시 켜면 작업 탭에 그대로 보입니다.
  창을 **X 로 닫으면** 진행 중인 작업도 같이 꺼집니다.
- `OpenMimic.bat` 은 `openmimic.ps1` 을 부릅니다 — `powershell -ExecutionPolicy Bypass -File .\openmimic.ps1 start` 와 같습니다.
- Intel 내장 GPU(XPU)로 학습하려면 설치할 때 PyTorch XPU 판을 고릅니다 — [INTEL.md](INTEL.md).
  내장 GPU 는 느리므로(Arc 140V · ACT · 카메라 3대 · 배치 8 에서 스텝당 약 2.3초) 긴 학습은 **HF Jobs** 를 권합니다.
- 실기 확인: Lunar Lake 노트북(Core Ultra 200V · Arc 140V · RAM 32 GB, Windows 11)에서 SO-ARM101 양팔 · 카메라 3대로
  설치 → 셋업 마법사 → 제어 → 수집 → XPU 학습 → OpenVINO 변환 → **NPU 실행**까지. 자세한 내용과 남은 확인 항목은 [WINDOWS_PORT.md](WINDOWS_PORT.md)

### 공통

- **다른 사람 환경을 바꾸지 않습니다** — conda 는 설치만 하고(`conda init` 안 함) 셸 설정·PATH 를 건드리지 않으며, 패키지는 전부 `openmimic` 가상환경 안에 들어갑니다
- 데이터·HF 캐시·캘리브레이션은 레포 폴더 `data/` 아래에 둡니다
- 업데이트·파일 위치·플랫폼 등 자세한 내용은 **[INSTALL.md](INSTALL.md)**

## 설치 상세

### 설치 스크립트가 하는 일 (Ubuntu)

```bash
mkdir -p ~/project && cd ~/project
git clone https://github.com/themakerrobot/openmimic.git openmimic
cd openmimic
chmod +x lerobot_conda.sh
sudo -v
nohup ./lerobot_conda.sh > /dev/null 2>&1 &
sleep 3; tail -f lerobot_conda.log      # "=== 완료 ===" 가 나올 때까지
```

conda 환경, PyTorch, lerobot(고정 커밋 `e40b58a`), SO-ARM101 용 Feetech · OMX 용 Dynamixel 패키지, 양팔 OMX 플러그인,
Diffusion·SmolVLA 패키지까지 설치합니다. 기기 종류는 자동으로 고릅니다.

| 플랫폼 | 고르는 조건 | PyTorch | 추론 |
|---|---|---|---|
| `intel` | Ubuntu · x86_64 + NVIDIA 없음 (Core Ultra 권장) | CPU 휠 + **OpenVINO / NNCF** | OpenVINO **NPU / GPU / CPU** |
| `win-intel` | Windows + NVIDIA 없음 (`install.ps1`) | CPU 휠 + **OpenVINO / NNCF** | OpenVINO **NPU / GPU / CPU** |
| `thor` | aarch64 (Jetson Thor) | CUDA 13 휠 | PyTorch (GPU) |
| `cuda` | Ubuntu · x86_64 + NVIDIA GPU | CUDA 13 휠 | PyTorch (GPU) |
| `win-cuda` | Windows + NVIDIA GPU (`install.ps1`) | CUDA 13 휠 (`torch<2.12 torchvision<0.27`) | PyTorch (GPU) |

셋업 화면 맨 아래 "4 · 기타" 에 이 기기의 플랫폼 이름이 나옵니다.

- 자동 판정이 틀리면 `OPENMIMIC_PLATFORM=intel ./lerobot_conda.sh` 처럼 지정합니다.
- Intel 내장 GPU 로 학습까지 해 보려면 `OPENMIMIC_PLATFORM=intel OPENMIMIC_TORCH=xpu ./lerobot_conda.sh` (**실험적** — [INTEL.md](INTEL.md)).
  이미 CPU 휠로 설치한 기기에서 다시 돌려도 됩니다 — 깔린 torch 종류가 다르면 지우고 XPU 휠로 바꿉니다.
- Ubuntu 22.04 / 24.04 / 26.04 대상. 파이썬은 conda 안의 3.12 라 우분투 기본 파이썬과 무관합니다.
  우분투 버전마다 이름이 바뀐 apt 패키지는 대체 이름으로 다시 시도하고, 없으면 경고만 남기고 계속합니다.

### 여러 사람이 쓰는 PC — 다른 사람 환경을 바꾸지 않습니다

- conda(miniforge)는 `~/miniforge3` 에 **설치만** 합니다. `conda init` 을 하지 않아 `~/.bashrc` 가 그대로이고, 터미널을 열어도 conda 가
  켜지지 않습니다. `~/.condarc` 도 쓰지 않습니다.
- torch·lerobot·openvino 등 파이썬 패키지는 전부 conda 환경 `openmimic` 안에만 들어갑니다. 시스템 python·pip 는 그대로입니다.
- 쓸 때만 `source ~/project/openmimic/activate.sh` 로 그 터미널에서 켭니다. 데이터·HF 캐시·torch 캐시도 레포 `data/` 안에 둡니다.
- 시스템에 하는 일: apt 기본 도구 몇 개(git, curl, build-essential, pkg-config, libgl1, libglib2.0, v4l-utils),
  시리얼 보드 udev 권한 규칙, 설치한 계정을 `dialout`·`video`(Intel 은 `render`) 그룹에 추가 — **재로그인 후** 반영.
- NPU·GPU 드라이버는 설치하지 않고 점검·안내만 합니다.
- 지우기: `rm -rf ~/project/openmimic ~/miniforge3` (다른 conda 를 이미 쓰고 있었다면 `~/miniforge3` 가 이 스크립트가 만든 것인지 확인 후)

## Windows 10 / 11 — `install.ps1`

Linux 와 같은 코드가 Windows 에서 네이티브로(WSL 없이) 돕니다. 한팔·양팔 모두 지원합니다. Lunar Lake 노트북(Core Ultra 200V · Arc 140V · RAM 32 GB)에서
SO-ARM101 양팔로 설치 → 셋업 마법사·캘리브레이션 → 제어 → 카메라 3대 수집 → XPU 학습 → OpenVINO 변환 → NPU 실행까지 실기로 확인했습니다. 남은 확인 항목은 [WINDOWS_PORT.md](WINDOWS_PORT.md) 에 있습니다.
설치가 끝나면 레포 폴더의 `OpenMimic.bat` 을 더블클릭해 켭니다.

```powershell
git clone https://github.com/themakerrobot/openmimic.git $HOME\project\openmimic
cd $HOME\project\openmimic
powershell -ExecutionPolicy Bypass -File .\install.ps1      # 로그: lerobot_conda.log, 끝에 "=== 완료 ==="
```

`lerobot_conda.sh` 와 같은 순서로 설치합니다 — miniforge(설치만), conda 환경 `openmimic`(Python 3.12), PyTorch(위 표),
lerobot 고정 커밋 `e40b58a`(`lerobot-src` 에 clone 후 `pip install -e`), Feetech·Dynamixel 패키지, `plugins/` 3종,
`win-intel` 이면 OpenVINO/NNCF. Linux 의 apt·udev·그룹 단계는 없습니다. 더해서:

- `pygrabber` — 카메라를 번호 대신 DirectShow 장치 경로로 기억합니다 (같은 모델 2대 구분, 재부팅 뒤 순서가 바뀌어도 같은 카메라).
  설치에 실패하면 번호로만 지정됩니다.
- `psutil` — 작업(수집·학습·실행) 프로세스 확인.
- 파이썬을 **UTF-8 모드**(`PYTHONUTF8=1`, `-X utf8`)로 돌립니다 — Windows 기본 인코딩(cp949·cp1252)으로는 한국어가 든 설정·로그를
  읽고 쓰다 깨지거나 멈춥니다.
- `git` 은 미리 있어야 합니다 (`winget install --id Git.Git -e`). 사용자 폴더 경로에 공백이 있으면 conda 를 설치할 수 없어 멈춥니다.
- 강제 지정: `$env:OPENMIMIC_PLATFORM='win-intel'` · Intel GPU 학습 시험: `$env:OPENMIMIC_TORCH='xpu'` (실험적 — 휠을 못 받으면 CPU 휠로 계속.
  이미 CPU 휠로 설치한 기기에서 다시 돌리면 CPU 휠을 지우고 XPU 휠로 바꿉니다. Core Ultra 200V 에서 설치·장치 인식 확인, 학습은 확인 전).
- 영상: lerobot 이 쓰는 `torchcodec` 은 지우고 PyAV 로 통일합니다 (Linux 와 같음). 디코딩은 torchcodec 이 없으면 lerobot 이 PyAV 로 바꾸고,
  인코딩(기본 libsvtav1)은 원래 PyAV 입니다.

| 무엇 | Windows 경로 |
|---|---|
| conda | `%USERPROFILE%\miniforge3` (환경: `envs\openmimic`) |
| 레포·데이터·설정 | Linux 와 같은 상대 경로 (`data\hf\…`, `outputs\…`, `openmimic_*.json`) |
| 수집 worker 상태 파일 | 레포 `openmimic_run\` (Linux 는 `/dev/shm/openmimic`) |
| 환경 켜기 | `. .\activate.ps1` (점 + 공백). 실행 정책이 막으면 `powershell -ExecutionPolicy Bypass -NoExit -Command ". .\activate.ps1"` |

```powershell
OpenMimic.bat                  # 탐색기에서 더블클릭 = 이 창에서 켜기 (cmd 에서 OpenMimic.bat update · stop · status … 도 됨)
.\openmimic.ps1 start          # 이 창에서 켜기 — 접속 주소 출력, 로그가 창에 보임, Ctrl+C 로 끔 (작업은 계속, 창을 닫으면 작업도 꺼짐)
.\openmimic.ps1 background     # 창 없이 켜기 — 로그 openmimic.log, pid openmimic_server.pid
.\openmimic.ps1 stop           # 끄기 (다른 창·백그라운드로 켠 것도) — 서버만 끄고 작업은 계속
.\openmimic.ps1 restart · update   # 끄고 이 창에서 다시 켜기 (update 는 git pull 먼저)
.\openmimic.ps1 status · log   # 상태 · 백그라운드 로그 보기
.\openmimic.ps1 autostart on   # 로그인 때 최소화된 창에서 켜기 (시작프로그램 폴더 바로 가기 — 관리자 권한 필요 없음)
```

실행 정책 때문에 막히면 앞에 `powershell -ExecutionPolicy Bypass -File` 을 붙입니다.

### Windows 에서 포트·카메라

- 시리얼 보드는 `COM5` 같은 이름으로 보이지만 이 번호는 꽂을 때마다 바뀔 수 있어 설정에는 **보드 시리얼 번호**(`usb-<vid>_<pid>_<번호>`),
  번호가 없는 보드는 **USB 위치**(`1-3.2` 같은 값)를 저장합니다 — Linux 의 by-id / by-path 와 같은 규칙. 실행할 때 지금의 COM 으로 찾습니다.
- 포트가 안 보이면: 장치 관리자 → **포트(COM & LPT)**. 느낌표가 있으면 보드 칩 드라이버(CH340/CH343 · CP210x)를 설치합니다.
  "액세스가 거부" 면 다른 프로그램(시리얼 모니터, 다른 OpenMimic)이 그 COM 을 잡고 있습니다.
- 카메라는 DirectShow 로 엽니다. 셋업 → 카메라에 `dshow:\\?\usb#vid_…` 같은 경로로 저장됩니다. 번호(`0`, `1`)로만 보이면
  `pygrabber` 가 없는 것입니다 (`. .\activate.ps1; pip install pygrabber`).
- 카메라가 안 열리면: 설정 → 개인 정보 → **카메라** 에서 데스크톱 앱 접근을 허용했는지 확인합니다.
- **카메라 여러 대(양팔 wrist ×2 + top 등)**: 셋업 → 카메라의 **형식을 MJPG** 로 두세요 (새로 추가하는 카메라는 기본이 MJPG, 예전에 추가한 카메라는
  경고 옆 **모두 MJPG 로** 버튼 → 설정 저장. MJPG 를 지원하지 않는 카메라는 열 때 오류가 나니 그 카메라만 형식을 '자동' 으로). 자동(대개 YUYV, 무압축)은 640×480 30fps 한 대가
  약 18 MB/s 라 USB 2.0 하나에 두세 대면 대역폭이 모자라 프레임이 끊기거나 열리지 않습니다. 가능하면 카메라를 서로 다른 USB 컨트롤러(PC 의 다른 쪽 포트)에 나눠 꽂습니다.
- **수집 주기**: 수집 화면에 실제 주기(`수집 주기 28.9 / 30 Hz`)가 보이고, 목표의 90% 아래면 경고합니다.
  낮으면 데이터셋 시간(프레임 번호 ÷ fps)이 실제보다 빨라져 학습한 정책이 실제보다 빠르게 움직입니다.
  수집 화면의 **녹화하면서 바로 영상 압축**(Windows 기본 켜짐)을 켜면 PNG 를 쓰지 않고 녹화 중에 압축해 에피소드 저장이 거의 바로 끝납니다
  (4코어 시험 PC 에서 카메라 3대 30fps 에 약 1.6 코어). CPU 가 못 따라가 프레임이 빠진 에피소드는 저장하지 않고 버립니다.
  끄면 에피소드 동안 카메라 프레임을 PNG 로 데이터셋 폴더에 쓰므로(초당 카메라 수 × fps 장), 실시간 검사(Windows Defender)가
  파일마다 검사해 CPU 를 씁니다. 주기가 모자라면 관리자 PowerShell 에서 이 폴더를 검사 예외로 둘 수 있습니다:
  `Add-MpPreference -ExclusionPath "$HOME\project\openmimic\data"` (보안 정책상 괜찮은지 먼저 확인하세요).

## 켜기 · 끄기 · 자동 시작 — `openmimic.sh`

```bash
cd ~/project/openmimic
./openmimic.sh start        # 켜기 (백그라운드, 로그 openmimic.log, pid openmimic_server.pid) — 접속 주소 출력
./openmimic.sh stop         # 끄기 — 수집·학습 같은 작업은 따로 돌아 그대로 남고, 다시 켜면 작업 탭에 보입니다
./openmimic.sh restart
./openmimic.sh status       # 켜져 있는지 · 접속 주소 · 자동 시작 여부
./openmimic.sh log          # tail -f openmimic.log
./openmimic.sh update       # git pull --ff-only 후 다시 켜기 (고친 파일이 있으면 멈춤)
./openmimic.sh autostart on # 로그인 때 자동으로 켜기 (systemd --user, ~/.config/systemd/user/openmimic.service)
./openmimic.sh autostart off
```

- 언제나 설치 스크립트가 만든 `activate.sh` 로 환경을 켜서 실행합니다 — 캘리브레이션·데이터 폴더가 설치 폴더(`data/hf`)로 맞춰집니다.
  `activate.sh` 없이 `python main.py` 로 켜면 lerobot 이 `~/.cache/huggingface` 같은 다른 캘리브레이션 폴더를 쓰게 되어,
  화면 위에 노란 경고 띠가 뜹니다(시작 로그에도 `!!` 로 남음).
- 자동 시작은 **이 계정에만** 걸립니다. 로그인하지 않은 상태에서 부팅 때도 켜려면 한 번 `sudo loginctl enable-linger $USER`.
  바탕화면 세션이 없는 SSH 전용 계정에서는 systemd 사용자 세션이 없어 켜지지 않을 수 있습니다 (확인 필요).
- 시작 로그(`openmimic.log`)에 `open : http://192.168.x.x:8080/` 처럼 이 PC 의 실제 주소가 찍힙니다.

## 이미 설치된 기기 업데이트

```bash
cd ~/project/openmimic && ./openmimic.sh update     # 보통은 이것만
```

새로 필요한 패키지가 생긴 업데이트라면 아래를 한 번 더 합니다.

```bash
cd ~/project/openmimic
source activate.sh
# OMX 를 처음 쓸 때 한 번만
pip install "dynamixel-sdk>=3.7.31,<3.9.0"
pip install --no-deps -e plugins/lerobot_robot_bi_omx -e plugins/lerobot_teleoperator_bi_omx
# 학습에서 ACT2 를 쓰려면 한 번만 (새 설치 스크립트는 같이 설치합니다)
pip install --no-deps -e plugins/lerobot_policy_act2
# 학습에서 Diffusion / SmolVLA 를 처음 쓸 때 한 번만 (화면의 '필요한 패키지 설치' 버튼으로도 됩니다)
(cd lerobot-src && pip install -e ".[diffusion,smolvla]")
# Intel 기기에서 OpenVINO 를 처음 쓸 때 한 번만
pip install "openvino>=2025.4" "nncf>=2.19"
```

패키지를 설치했으면 `./openmimic.sh restart` 로 다시 켜고, 브라우저는 강력 새로고침(Ctrl+Shift+R) 하세요.

## 파일이 놓이는 곳

| 무엇 | 경로 (`~/project/openmimic/` 아래) |
|---|---|
| 데이터셋 | `data/hf/lerobot/local/<이름>/` |
| 캘리브레이션 | `data/hf/lerobot/calibration/robots/so_follower/<캘리브 id>.json` 등 (기종·역할별 폴더) |
| 학습한 모델 | `outputs/<이름>/checkpoints/<step>/pretrained_model/` |
| 설정 · 환경 · 프로젝트 | `openmimic_config.json`, `openmimic_envs/`, `openmimic_projects.json` |
| 전원 점검 기록 (팔별 마지막 서보 전압) | `openmimic_power.json` |
| 작업 로그 | `openmimic_jobs/<작업 id>.log` |
| OpenMimic 자체 로그 · pid | `openmimic.log` · `openmimic_server.pid` (`openmimic.sh` 가 씀) |

## 화면 언어

`OPENMIMIC_LANG=en ./openmimic.sh start` 로 켜면 처음부터 영어로 열립니다. 번역은 `openmimic_i18n_en.json` 사전 하나로 하고,
고치면 재시작 없이 다음 새로고침부터 반영됩니다. 사전에 없는 문장은 한국어로 남습니다.

## 접속 보안

기본은 인증 없이 `http://<host>:8080/` 으로 들어갑니다. 필요하면 토큰을 켭니다.

```bash
OPENMIMIC_TOKEN=원하는값 ./openmimic.sh start    # 토큰 직접 지정
OPENMIMIC_AUTH=on        ./openmimic.sh start    # openmimic_token.txt 에 자동 생성
```

인증과 별개로, 다른 웹사이트가 브라우저를 통해 몰래 보내는 요청은 받지 않습니다.

## 터미널 도구

### 팔 불량 점검

셋업 탭의 팔 불량 점검과 같은 판정을 터미널에서 돌립니다 (OpenMimic 이 그 포트를 잡고 있지 않을 때). 서보에 아무것도 쓰지 않습니다.

```bash
python tools_armcheck.py --port /dev/serial/by-id/usb-... --role follower   # SO-ARM101
python tools_dxlcheck.py --port /dev/serial/by-id/usb-... --role follower   # OMX
python tools_armcheck.py --port COM5 --role follower                         # Windows
```

종료 코드: `0` 정상 / `1` 주의 / `2` 불량 의심 / `3` 실행 실패. `--json` 으로 기록용 출력.

### 데이터셋 복구

Datasets 화면의 **복구** 버튼과 같은 동작입니다: `python tools_dsrepair.py <데이터셋 폴더>` (고치기 전에 백업합니다).
