<h1 align="center">OpenMimic</h1>

<p align="center">
  <b>한국어</b> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <b>로봇팔 셋업 · 데이터 수집 · 학습 · 실행을 웹 화면 하나로</b><br>
  SO-ARM101 · ROBOTIS OMX · 한팔 / 양팔 · Intel Core Ultra NPU · Jetson Thor · NVIDIA GPU<br>
  Ubuntu 22.04 · 24.04 · 26.04 · Windows 10 · 11 (네이티브)
</p>

<p align="center">
  <img src="docs/img/arms.gif" alt="SO-ARM101 과 OMX" width="880">
</p>

<p align="center">
  <a href="#빠른-시작">빠른 시작</a> ·
  <a href="#처음-셋업--셋업-마법사">셋업 마법사</a> ·
  <a href="#화면별-사용법">화면별 사용법</a> ·
  <a href="#팔이-여러-세트일-때">팔 여러 세트</a> ·
  <a href="#intel-pc-에서-npu-로-실행">Intel NPU</a> ·
  <a href="#문제-해결">문제 해결</a>
</p>

---

HuggingFace [lerobot](https://github.com/huggingface/lerobot) 으로 로봇팔을 다루는 과정을 **명령어 없이 브라우저에서** 합니다.
같은 네트워크의 노트북·태블릿에서 접속해도 되고, 팔·카메라·학습은 OpenMimic 을 켠 PC 에서 돕니다.

| ① 셋업 | ② 수집 | ③ 학습 | ④ 실행 |
|:---:|:---:|:---:|:---:|
| ![](docs/img/wizard_diag.png) | ![](docs/img/collect_run.png) | ![](docs/img/training.png) | ![](docs/img/rollout_live.png) |
| 포트 찾기 · 진단 · 캘리브레이션을 손으로 움직이며 | 리더 팔로 시연, 키 하나로 녹화·저장 | ACT · ACT2 · Diffusion · SmolVLA | 카메라 · 3D · 관절을 보며 자율 구동, 성공률 기록 |

## 지원 기종

| 기종 | 모터 | 한팔 | 양팔 | 캘리브레이션 |
|---|---|:---:|:---:|---|
| **SO-ARM101** (SO-101) | Feetech STS3215 | ✅ | ✅ | 관절을 손으로 끝까지 움직여 기록 |
| **OMX** (ROBOTIS OMX-F / OMX-L) | Dynamixel XL430 / XL330 | ✅ | ✅ | 공장값 쓰기 (버튼 하나) |

| 기기 | OS | 학습 | 실행(추론) |
|---|---|---|---|
| Intel Core Ultra PC | Ubuntu · Windows | 내장 GPU(XPU — 느림) 또는 HF Jobs 클라우드 | ✅ **NPU** / GPU / CPU (OpenVINO, ACT · ACT2) |
| NVIDIA GPU PC | Ubuntu · Windows | ✅ GPU | ✅ GPU |
| Jetson Thor | Ubuntu (JetPack) | ✅ GPU | ✅ GPU |

**Ubuntu 와 Windows 를 대등하게 지원합니다.** Windows 는 WSL·가상머신 없이 네이티브로 돌고, 한팔·양팔과 셋업 → 수집 → 학습 → 실행 전 과정이 같습니다.
같은 코드가 두 OS 에서 돌고 데이터셋·캘리브레이션·모델 파일 형식도 같아, Ubuntu 와 Windows 사이에 그대로 옮겨 씁니다.
설치 스크립트가 기기 종류(Intel / Thor / NVIDIA)를 자동으로 고릅니다.

## 빠른 시작

| OS | 설치 | 켜기 |
|---|---|---|
| Ubuntu 22.04 · 24.04 · 26.04 | `lerobot_conda.sh` | `./openmimic.sh start` |
| Windows 10 · 11 (64비트) | `install.ps1` | 레포 폴더의 **`OpenMimic.bat`** 더블클릭 |

켜면 접속 주소가 나옵니다. 브라우저에서 **`http://<PC IP>:8080`** 으로 들어가면 처음엔 **셋업 마법사**가 바로 뜨고,
그다음부터는 데이터셋 화면 위의 **시작하기** 안내(셋업 → 캘리브레이션 → 수집 → 학습 → 실행)를 따라가면 됩니다.
같은 네트워크의 노트북·태블릿에서 접속해도 됩니다.

### Ubuntu

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

### Windows 10 / 11

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
- Intel 내장 GPU(XPU)로 학습하려면 설치할 때 PyTorch XPU 판을 고릅니다 — [docs/INTEL.md](docs/INTEL.md).
  내장 GPU 는 느리므로(Arc 140V · ACT · 카메라 3대 · 배치 8 에서 스텝당 약 2.3초) 긴 학습은 **HF Jobs** 를 권합니다.
- 실기 확인: Lunar Lake 노트북(Core Ultra 200V · Arc 140V · RAM 32 GB, Windows 11)에서 SO-ARM101 양팔 · 카메라 3대로
  설치 → 셋업 마법사 → 제어 → 수집 → XPU 학습 → OpenVINO 변환 → **NPU 실행**까지. 자세한 내용과 남은 확인 항목은 [docs/WINDOWS_PORT.md](docs/WINDOWS_PORT.md)

### 공통

- **다른 사람 환경을 바꾸지 않습니다** — conda 는 설치만 하고(`conda init` 안 함) 셸 설정·PATH 를 건드리지 않으며, 패키지는 전부 `openmimic` 가상환경 안에 들어갑니다
- 데이터·HF 캐시·캘리브레이션은 레포 폴더 `data/` 아래에 둡니다
- 업데이트·파일 위치·플랫폼 등 자세한 내용은 **[docs/INSTALL.md](docs/INSTALL.md)**

## 처음 셋업 — 셋업 마법사

**셋업 → 셋업 마법사 시작**. 맨 위에서 기종과 한팔/양팔을 고르고, 팔 칸(양팔이면 4개)을 하나씩 누르면 안 끝난 단계부터 이어집니다.
모터를 구동하는 단계는 없습니다 — 전부 **손으로 움직여서** 확인합니다.

1. **포트 찾기** — 감시를 켜고 그 팔만 손으로 움직이면 움직인 포트가 초록으로 추천됩니다. ⚠️ 감시 중엔 토크가 꺼지니 팔로워를 받치세요
2. **진단** — 모터 응답·전압·온도·보호 플래그. 모터가 안 보이면 그 자리에서 **모터 ID 세팅**
3. **캘리브레이션** — SO-ARM101 은 관절마다 끝까지 천천히, OMX 는 **공장값 쓰기** 버튼 하나.
   다른 PC 에서 캘리브레이션한 SO-ARM101 이면 **보드에서 가져오기** 로 건너뜁니다
4. **확인** — 손으로 움직였을 때 3D 가 실물과 같이 움직이면 **일치함 — 완료**

| 캘리브레이션 (SO-ARM101) | 확인 |
|---|---|
| ![](docs/img/wizard_calib.png) | ![](docs/img/wizard_verify.png) |

| OMX 공장값 쓰기 | OMX 확인 |
|---|---|
| ![](docs/img/wizard_omx_factory.png) | ![](docs/img/wizard_omx_verify.png) |

> ⚡ **전원 먼저** — 팔로워와 리더 어댑터를 바꿔 꽂지 마세요. 정격 전압은 서보 라벨·키트 사양으로 확인합니다
> (STS3215 는 7.4 V 판과 12 V 판이 따로 있습니다). 진단 단계의 전압이 어댑터와 다르면 거기서 멈추세요.

카메라는 **셋업 → 3 · 카메라** 에서 스캔하면 썸네일이 찍혀 어느 장치가 어느 카메라인지 보고 등록할 수 있습니다.

## 화면별 사용법

위쪽 탭은 작업 순서대로 **셋업 · 캘리브레이션 · 제어 · 수집 · 데이터셋 · 학습 · 모델 · 실행** 이고,
**더보기** 아래에 프로젝트 · 허브 · 작업이 있습니다. 먼저 해야 할 일이 남아 있으면(예: 포트 미설정, 데이터셋 없음)
그 화면의 시작 버튼이 꺼지고 **어디로 가면 되는지 큰 버튼**이 뜹니다.

### 셋업

맨 위 **셋업 마법사 시작** 이 처음 연결하는 길이고, 이 화면은 카메라·환경 등록과 세부 설정용입니다.

| 칸 | 하는 일 |
|---|---|
| 0 · 환경 | 하드웨어 구성 한 벌(기종·모드·포트·캘리브 id·카메라)을 이름 붙여 저장·전환 |
| 1 · 기종 · 모드 | SO-ARM101 / OMX, 한팔 / 양팔. 팔별 포트·캘리브 id·3D 배치 |
| 2 · USB 시리얼 포트 | 포트 스캔, **포트 감시**로 리더/팔로워 판별. 모터 ID 세팅·팔 불량 점검은 접힌 칸(2b · 2c)에 |
| 3 · 카메라 | 스캔(썸네일)·등록, 해상도·fps·형식 — USB 카메라 여러 대가 한 허브에서 끊기면 **MJPG** |
| 4 · 기타 | fps, 한 번에 움직일 최대 크기(`max_relative_target`), 기본 태스크 |

값을 바꾸면 **설정 저장**을 눌러야 적용됩니다.

![셋업](docs/img/setup.png)

### 캘리브레이션

캘리브레이션만 따로 할 때와, **캘리브레이션 파일을 관리**할 때 씁니다. ⚠️ 시작하면 토크가 꺼집니다 — 팔로워는 받치세요.
아래 **캘리브레이션 파일 모음** 에 이 PC 의 모든 팔 파일이 보이고, zip 으로 **내보내기 / 가져오기** 합니다. 안 쓰는 파일은 **삭제** 로 지웁니다 (같은 폴더에 `.json.bak` 으로 남김).

![캘리브레이션](docs/img/calib.png)

### 제어

팔로워 수동 제어. 탭에 들어오면 자동으로 연결되고, 떠나면 토크를 끄고 연결을 끊습니다.

- **토크 ON** 후 슬라이더로 이동, **리더 팔로우** 로 리더를 따라가게, **E-STOP** 으로 즉시 토크 해제
- 1초 넘게 명령을 못 따라가는 관절(막힘·보호)과 과열은 빨갛게 표시됩니다. 양팔에서 한 팔만 토크가 안 켜지면 그 팔에 따로 경고가 뜹니다
- 다른 탭으로 옮기거나 탭을 닫으면 토크를 끄고 연결을 풉니다 — 그래야 수집·실행을 시작할 수 있습니다

![제어](docs/img/control.png)

### 수집

수집 설정을 넣고 시작합니다. **자동으로 녹화되지 않습니다** — 대기 중에 키로 시작합니다.

| 키 | 동작 |
|---|---|
| `Space` · `→` · `s` | 녹화 시작 (대기 중) / 저장하고 다음 (녹화 중) |
| `←` · `Backspace` · `r` | 버리고 다시 (녹화 중) |
| `Esc` | 수집 끝내기 |

- **신호음**으로 시작·저장·마지막 3초를 알려 줘 화면을 안 봐도 됩니다
- 기존 데이터셋에 **이어서 수집**도 됩니다
- **녹화하면서 바로 영상 압축**(Windows 기본 켜짐)을 켜면 에피소드 저장이 거의 바로 끝납니다. CPU 가 못 따라가 프레임이 빠진 에피소드는 버립니다
- 진행 화면에 실제 **수집 주기**(예: `29.8 / 30 Hz`)가 보이고, 목표보다 낮으면 경고합니다 — 낮으면 데이터의 시간이 실제보다 빨라집니다

| 수집 설정 | 수집 진행 |
|---|---|
| ![](docs/img/collect.png) | ![](docs/img/collect_run.png) |

### 데이터셋

처음 화면입니다. 위에 **시작하기** 안내가 남은 단계를 보여 주고(다 끝나거나 숨기면 사라짐), 아래가 데이터셋 목록입니다.
이름을 누르면 리뷰 화면이 열립니다 — 카메라 전부를 한 시계로 맞춘 재생, 3D 재생, 관절 그래프.

- ← / → 에피소드 이동, Space 재생, **, / .** 한 프레임, **X** 불량 표시 → 일괄 삭제
- 다운로드 · 다른 기기에서 **가져오기** · 끊긴 데이터셋 **복구**

| 목록 | 리뷰 |
|---|---|
| ![](docs/img/datasets.png) | ![](docs/img/review.png) |

### 학습

데이터셋과 **정책**을 골라 학습합니다. loss·grad norm·학습률 그래프와 **진행률·남은 시간**이 실시간으로 보입니다.

| 정책 | 언제 |
|---|---|
| **ACT** (기본) | 데모 50개 안팎, 가볍고 빠름 — Intel NPU 로 실행 가능 |
| **ACT2** (실험) | ACT 에 영상 증강·청크 보간·DINOv2 백본·flow 헤드를 켜고 끄며 성능을 비교 — Intel NPU 로 실행 가능 |
| **Diffusion** | 동작이 여러 갈래인 태스크 |
| **SmolVLA** | 태스크 설명(언어)을 쓰는 450M 사전학습 모델 미세조정 (GPU 권장) |

- 데이터셋·정책 조합이 안 맞으면 **시작 전에** 빨간 표시로 알려 줍니다 (카메라 해상도, 한팔/양팔 등)
- 에피소드 10개 · 수천 step 은 흐름 확인용입니다. 실제로 집게 하려면 데모 **50개 안팎**과 충분한 step(수만)이 필요합니다 — 내장 GPU 에선 오래 걸리니 HF Jobs 를 권합니다
- **실행 위치**에서 **HF Jobs** 를 고르면 Hugging Face 클라우드 GPU 로 학습합니다 (유료, 시간당 요금이 목록에 보임)
- 정책별 자세한 내용은 **[docs/POLICIES.md](docs/POLICIES.md)**

![학습](docs/img/training.png)

### 모델

학습한 모델 하나가 카드 하나입니다 — 진행 step, 마지막 loss, **실기 성공률**, OpenVINO 변환 상태.
체크포인트마다 **실행 · OpenVINO 변환 · 내보내기 · 삭제**, 카드마다 **이어서 학습**. 맨 위에서 다른 기기의 모델을 **가져오기** 합니다.

![모델](docs/img/models.png)

### 실행

체크포인트를 골라 자율 구동합니다. 시작 전에 카메라·관절 수를 지금 셋업과 대조해 안 맞으면 팔이 움직이기 전에 막습니다.

- 실시간 화면: 카메라 · 3D 팔 · 관절별 실측/명령/차이 · 제어 주기 · 추론 시간
- 시도마다 **성공 (S) / 실패 (F)** 를 누르면 체크포인트별 실기 성공률이 쌓입니다
- 실패하면 로그에서 **원인 추정** (모터 무응답·과부하·포트·카메라·캘리브레이션 등)

| 시작 | 실행 중 |
|---|---|
| ![](docs/img/rollout.png) | ![](docs/img/rollout_live.png) |

### 더보기 — 프로젝트 · 허브 · 작업

- **프로젝트** — 태스크 하나를 묶는 단위(태스크 설명, 프로젝트 전용 환경, 데이터셋, 모델). 새 프로젝트를 만들면 같은 이름의 빈 환경
  (기종·한팔/양팔만 정하고 포트·카메라는 비움, 캘리브 id 는 `<프로젝트>_follower` 처럼 전용)이 생기고 셋업 마법사로 넘어가 처음부터 연결합니다.
  프로젝트를 열면 그 환경으로 바뀌고 다른 화면이 그 프로젝트 기준으로 보이며, 새 데이터셋·모델이 자동으로 들어갑니다.
  프로젝트를 지우면 그 환경도 지웁니다 (데이터셋·모델·캘리브레이션 파일은 남음). 없어도 다른 기능은 그대로 됩니다.
- **허브** — Hugging Face Hub 와 주고받습니다. 로그인(토큰), 데이터셋 올리기, 데이터셋·모델 받기, **HF Jobs 클라우드 학습** 확인·취소.
- **작업** — 수집·학습·실행 같은 백그라운드 작업 목록과 로그. 여기서 중지할 수 있습니다.

| 프로젝트 | 허브 |
|---|---|
| ![](docs/img/projects.png) | ![](docs/img/hub.png) |

## 팔이 여러 세트일 때

캘리브레이션 파일은 **캘리브 id** 이름으로 저장됩니다. 세트마다 id 를 다르게 두면 파일이 섞이지 않습니다.

1. 팔에 이름표를 붙이고 (예: `b2`), **셋업 → 1 · 기종·모드** 에서 캘리브 id 를 `b2_follower_left` 처럼 바꿔 **설정 저장**
   - 같은 팔(같은 포트)의 id 만 바꾸면 기존 캘리브레이션 파일이 **새 이름으로 자동 복사**됩니다
   - 양팔은 같은 역할의 왼팔·오른팔 앞부분이 같아야 합니다 (`b2_follower` + `_left` / `_right`)
2. **셋업 → 0 · 환경** 에서 이 구성을 `bench2` 처럼 저장 — 다음부터는 환경만 바꾸면 그 세트의 파일을 씁니다
3. 다른 PC 로 옮길 때는 **캘리브레이션 → 내보내기 (zip)** → 다른 PC 의 **가져오기**. SO-ARM101 은 팔만 옮겨 **보드에서 가져오기** 도 됩니다

## Intel PC 에서 NPU 로 실행

Intel Core Ultra PC 에서도 셋업·수집·리뷰는 똑같고, **ACT · ACT2** 모델을 OpenVINO 로 바꿔 **NPU** 에서 실행합니다.

1. 학습 — 이 PC 의 내장 GPU(XPU, 느림)로 학습하거나, HF Jobs(클라우드)·NVIDIA 기기에서 학습한 모델을 **모델 → 가져오기**
2. **모델 → OpenVINO 변환** — NPU · GPU · CPU 마다 오차와 추론 시간을 표로 보여 줍니다
3. **실행 → 추론 엔진: OpenVINO · NPU**

자세한 내용과 실측값은 **[docs/INTEL.md](docs/INTEL.md)**

## 서보 상태 감시 · 전원 점검

저가 서보 팔에서 실제로 고장이 나는 지점(어댑터 혼용, 과열, 전원 강하)을 OpenMimic 이 지켜봅니다.

- **시작 전 전원 점검** — 제어·수집·실행을 시작할 때 팔마다 서보 전압을 읽습니다.
  - **막음**: SO-ARM101 리더에 12V 가 들어옴(어댑터가 바뀜), 서보 자체 전압 한계를 넘음
  - **경고**: 팔로워가 5V, 범위 밖, 지난번과 전압이 크게 다름
- **실행 중 감시** — 1초마다 팔로워의 온도·전압·보호 플래그를 읽습니다.
  - **실행**: 서보 온도 한계에 가깝거나, 전압이 크게 떨어지거나, 서보 보호가 걸리면 **자동 정지**(중지 버튼과 같음)
  - **수집**: 같은 조건에서 녹화 중이던 에피소드를 버리고 수집을 끝냅니다
  - **제어**: 팔이 떨어지지 않게 토크는 그대로 두고 리더 팔로우·슬라이더만 멈춥니다
- 판정 기준은 서보에 저장된 한계값과 출처가 있는 값만 씁니다 — 자세히는 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#서보-상태-감시--전원-점검)

## 화면 언어 — 한/EN

위쪽 막대의 **한/EN** 버튼으로 한국어 ↔ 영어를 바꿉니다. 직접 입력한 글(태스크 설명 등)은 번역하지 않습니다.

![영어 화면](docs/img/english.png)

## 문제 해결

| 증상 | 확인 |
|---|---|
| 포트가 안 보임 | USB 다시 꽂기, 설치 후 재로그인 했는지 (`groups` 에 `dialout`). Windows 는 장치 관리자의 **포트(COM & LPT)** 에 보이는지, 드라이버(CH340/CH343 · CP210x) |
| Windows: `'pwsh' is not recognized` | PowerShell 7 이 없는 것입니다 — `powershell` 로 실행하거나 `OpenMimic.bat` 을 쓰세요 |
| Windows: 스크립트 실행이 막힘 (실행 정책) | `powershell -ExecutionPolicy Bypass -File .\openmimic.ps1 start` 처럼 앞에 붙이거나 `OpenMimic.bat` 을 쓰세요 |
| Windows: 다른 PC·태블릿에서 접속이 안 됨 | 처음 켤 때 뜬 방화벽 창에서 허용했는지 — 설정 → 개인 정보 및 보안 → Windows 보안 → 방화벽에서 Python 허용 |
| Windows: 카메라가 안 열림 | 설정 → 개인 정보 → **카메라** 에서 데스크톱 앱 접근 허용, 다른 프로그램(화상회의 등)이 카메라를 쓰는지 |
| Windows: 수집 주기 경고 | 카메라 형식 MJPG, 카메라를 다른 USB 포트로 나눠 꽂기. 그래도 낮으면 [INSTALL.md](docs/INSTALL.md#windows-에서-포트카메라) 의 실시간 검사 예외 |
| 재부팅 뒤 팔·카메라가 바뀜 (Windows) | 시리얼 번호 없는 보드·카메라는 **꽂은 USB 구멍**을 따라갑니다 — 항상 같은 구멍에. 카메라가 번호로만 잡히면 `pip install pygrabber` |
| "캘리브레이션 파일이 없습니다" | 셋업 마법사 ③ 또는 캘리브레이션 탭. 다른 PC 의 파일이면 캘리브레이션 → 가져오기 |
| 3D 가 안 나옴 | 3D 는 인터넷(CDN)에서 받습니다. 인터넷이 없으면 3D 칸만 빠지고 나머지는 동작 |
| 카메라 여러 대가 끊김 | 셋업 → 카메라 **형식** 을 MJPG 로, 카메라는 PC 본체 포트에 |
| "다른 작업 실행 중" / "실행 중: manual-control" | 제어 탭이 열려 있거나(다른 기기·브라우저 포함 — 닫으면 풀림) 수집·감시·캘리브레이션이 도는 중 — 더보기 → 작업에서 끝내기 |
| 데이터셋에 "마무리 안 됨" | 데이터셋 목록의 **복구** |
| 실행이 바로 끝남 | 실행 화면의 **원인 추정** 과 로그 |
| "정책이 카메라 … 를 씁니다" / "관절 수 …" | 학습 때와 카메라 이름·한팔/양팔이 다름 — 셋업을 학습 때와 같게 |
| 업데이트 후 화면이 이상함 | `./openmimic.sh restart` (Windows 는 `OpenMimic.bat restart`) + 브라우저 강력 새로고침 (Ctrl+Shift+R) |
| 화면 위에 "캘리브레이션 폴더가 설치 폴더가 아니라 …" | `activate.sh` 없이 켠 경우입니다. 끄고 `./openmimic.sh start` 로 다시 켜세요 (Windows 는 `OpenMimic.bat`) |
| "전원 점검 실패 — 시작하지 않았습니다" | 리더·팔로워 어댑터가 바뀌었거나 전압이 서보 한계 밖입니다. **전원부터 분리**하고 어댑터 라벨을 확인하세요 |
| "서보 상태 이상으로 자동 정지" | 과열이면 식힌 뒤 막힘·과부하 원인을, 전압 강하면 어댑터 전류 용량·커넥터를 확인하세요 |

## 문서

| 문서 | 내용 |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | 설치 스크립트가 하는 일(Ubuntu · Windows), 업데이트, 파일 위치, 보안, 터미널 도구 |
| [docs/POLICIES.md](docs/POLICIES.md) | 학습 정책 비교, 양팔 지원, 넣지 않은 정책 |
| [docs/INTEL.md](docs/INTEL.md) | Intel Core Ultra — OpenVINO 변환·NPU 추론, XPU 학습, 실측값 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 구조·설계 이유·설정 파일 형식·판정 근거 (개발자용) |
| [docs/WINDOWS_PORT.md](docs/WINDOWS_PORT.md) | Windows 지원 — OS 차이를 메운 방법, 실기에서 확인한 것과 남은 확인 항목 |

## License

- 코드: MIT — [LICENSE](LICENSE)
- `urdf/` SO-101 URDF·STL: [TheRobotStudio SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100) (Apache 2.0) — [urdf/LICENSE.md](urdf/LICENSE.md)
- `urdf/omx_f.urdf`, `urdf/open_manipulator_description/`: [ROBOTIS open_manipulator](https://github.com/ROBOTIS-GIT/open_manipulator) (Apache 2.0) — [urdf/LICENSE-omx.md](urdf/LICENSE-omx.md)
- `plugins/`: 양팔 OMX 는 lerobot `bi_so_follower` / `bi_so_leader` 구조를 따르고, ACT2 는 lerobot ACT 를 바탕으로 한 코드 (Apache 2.0)
- `tools_dsrepair.py`: [huggingface/leLab](https://github.com/huggingface/leLab) `dataset_repair.py` 를 옮겨 고친 코드 (Apache 2.0, 파일 머리에 표기)
